"""The schedule catalog and the reconciler that makes the server match it.

Every recurring job is one ``ScheduleEntry`` here: its id is the name the job
has always had, its spec the same cron / interval, its policy ``overlap=SKIP`` (a
tick that fires while the previous run is still going is dropped, never
stacked — the advisory lock inside the activity is the second line of defence)
and a catch-up window sized to its cadence, so a daily job missed while the
*server* was down still fires when it returns while a 30 s drain simply waits
for its next tick. Worker downtime never loses a tick at all: the workflow task
sits on the queue until a worker polls it.

``sync_schedules`` is idempotent and runs on every default-queue worker boot
and from ``python -m worker schedules sync``. It creates what is missing,
updates what drifted (a spec change ships as a code change), deletes managed
schedules that left the catalog, and never touches a schedule it did not
create — ownership is a memo the reconciler stamps, so a shared namespace is
safe. That holds on the create path too: a catalog id already taken by a
schedule stamped by someone else is reported ``foreign`` and left alone rather
than overwritten. An entry whose workflow no worker in this codebase can serve
yet is reported as pending and not created: a schedule for an unserved workflow
would only pile up runs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from functools import cache
from typing import Any

from alkera_core.config import settings
from alkera_core.extensions import ExtensionPoint
from alkera_core.logging import get_logger
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)

log = get_logger(__name__)

MANAGED_BY = "alkera-worker"
"""The memo value that marks a schedule as ours. Foreign schedules are never touched."""
MANAGED_MEMO_KEY = "managed_by"
MANAGED_NOTE = "managed by alkera-worker; edit apps/worker/worker/schedules.py"

CATCHUP_TIGHT = timedelta(minutes=1)
CATCHUP_HOURLY = timedelta(hours=1)
CATCHUP_DAILY = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One recurring job. Exactly one of ``cron`` (five-field, UTC) or ``every``."""

    id: str
    workflow: StrEnum
    cron: str | None = None
    every: timedelta | None = None
    catchup_window: timedelta = CATCHUP_HOURLY
    execution_timeout: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if (self.cron is None) == (self.every is None):
            raise ValueError(f"schedule {self.id!r} needs exactly one of cron= or every=")
        if self.cron is not None:
            calendar_from_cron(self.cron)  # refuse an unsupported expression at import
        if self.every is not None and self.every <= timedelta(0):
            raise ValueError(f"schedule {self.id!r} interval must be positive")

    @property
    def queue(self) -> TaskQueue:
        return QUEUE_FOR[self.workflow]

    @property
    def spec_text(self) -> str:
        """The human spelling for listings: the cron, or ``every 30s``."""
        if self.cron is not None:
            return self.cron
        assert self.every is not None
        return f"every {int(self.every.total_seconds())}s"

    def to_schedule(self) -> Schedule:
        spec = (
            ScheduleSpec(cron_expressions=[self.cron])
            if self.cron is not None
            else ScheduleSpec(intervals=[ScheduleIntervalSpec(every=self.every or timedelta())])
        )
        return Schedule(
            action=ScheduleActionStartWorkflow(
                self.workflow.value,
                id=self.id,
                task_queue=self.queue.value,
                execution_timeout=self.execution_timeout,
            ),
            spec=spec,
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=self.catchup_window,
                pause_on_failure=False,
            ),
            state=ScheduleState(note=MANAGED_NOTE),
        )


# --- cron → the server's calendar form ---------------------------------------

_CRON_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 6),
)


def _cron_field(text: str, name: str, lo: int, hi: int) -> ScheduleRange:
    if text == "*":
        return ScheduleRange(start=lo, end=hi, step=1)
    if text.startswith("*/"):
        step = int(text[2:])
        if step < 1:
            raise ValueError(f"cron {name} step must be positive: {text!r}")
        return ScheduleRange(start=lo, end=hi, step=step)
    value = int(text)
    if not lo <= value <= hi:
        raise ValueError(f"cron {name} {value} is outside {lo}..{hi}")
    return ScheduleRange(start=value, end=value, step=1)


def calendar_from_cron(cron: str) -> ScheduleCalendarSpec:
    """The structured calendar the server stores for a five-field cron.

    The server keeps a cron as a ``ScheduleCalendarSpec`` and describes it back
    that way, never as the string; this is the same normalisation so a desired
    entry and a described schedule compare equal. Only the grammar the catalog
    uses is accepted — ``*``, a number, ``*/n`` — anything else is refused rather
    than guessed at.
    """
    tokens = cron.split()
    if len(tokens) != 5:
        raise ValueError(f"cron must have five fields (minute hour dom month dow): {cron!r}")
    ranges = {
        name: (_cron_field(text, name, lo, hi),)
        for text, (name, lo, hi) in zip(tokens, _CRON_FIELDS, strict=True)
    }
    return ScheduleCalendarSpec(
        second=(ScheduleRange(start=0, end=0, step=1),),
        minute=ranges["minute"],
        hour=ranges["hour"],
        day_of_month=ranges["day_of_month"],
        month=ranges["month"],
        year=(),
        day_of_week=ranges["day_of_week"],
    )


_OPEN_SCHEDULES: tuple[ScheduleEntry, ...] = (
    ScheduleEntry(
        "prune-expired-tokens",
        WorkflowType.PRUNE_EXPIRED_TOKENS,
        cron="0 3 * * *",
        catchup_window=CATCHUP_DAILY,
    ),
    ScheduleEntry(
        "prune-expired-device-codes",
        WorkflowType.PRUNE_EXPIRED_DEVICE_CODES,
        cron="15 3 * * *",
        catchup_window=CATCHUP_DAILY,
    ),
    ScheduleEntry(
        "prune-login-lockouts",
        WorkflowType.PRUNE_LOGIN_LOCKOUTS,
        cron="30 3 * * *",
        catchup_window=CATCHUP_DAILY,
    ),
    ScheduleEntry(
        "prune-identity-security-events",
        WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS,
        cron="40 3 * * *",
        catchup_window=CATCHUP_DAILY,
    ),
    ScheduleEntry(
        "sweep-notebook-runs",
        WorkflowType.SWEEP_NOTEBOOK_RUNS,
        every=timedelta(seconds=60),
        catchup_window=CATCHUP_TIGHT,
    ),
    ScheduleEntry(
        "entitlements-watchdog",
        WorkflowType.ENTITLEMENTS_WATCHDOG,
        cron="0 1 * * *",
        catchup_window=CATCHUP_DAILY,
    ),
    ScheduleEntry(
        "deployment-health",
        WorkflowType.DEPLOYMENT_HEALTH,
        cron="*/5 * * * *",
        catchup_window=CATCHUP_TIGHT,
    ),
    # The compute plane bills whole minutes off a high-water mark, so the tick is a
    # minute and a missed one self-heals on the next.
    ScheduleEntry(
        "compute-meter",
        WorkflowType.COMPUTE_METER,
        every=timedelta(seconds=60),
        catchup_window=CATCHUP_TIGHT,
    ),
    # The only pass that looks from the PROVIDER inwards: a pod whose id never
    # reached the database is invisible to everything else and bills until
    # somebody opens the provider's console. Its period is a setting because it
    # trades one provider list call against how long a leaked pod runs.
    ScheduleEntry(
        "compute-reconcile",
        WorkflowType.COMPUTE_RECONCILE,
        every=timedelta(seconds=settings.compute_reconcile_period_seconds),
        catchup_window=CATCHUP_TIGHT,
    ),
    # The sweep is the ONLY thing that announces a box which stopped
    # heartbeating — a heartbeat cannot report its own absence — so its period
    # is added to the reachability window to give the delay between a box dying
    # and the reader being told. The kill drill budgets 60 s for that, which a
    # five-minute sweep could not meet by an order of magnitude. One indexed
    # read per tick, and a tick that finds no transition emits nothing, so
    # running it inside the window is cheap; overlapping ticks are harmless.
    ScheduleEntry(
        "compute-sweep",
        WorkflowType.COMPUTE_SWEEP,
        every=timedelta(seconds=15),
        catchup_window=CATCHUP_TIGHT,
    ),
    # Every org machine converged on what its org asked for: started, replaced,
    # stopped, released. Thirty seconds, so a purchase starts and a stop lands
    # promptly; a pass that finds nothing to do is a few indexed reads.
    ScheduleEntry(
        "compute-org-machine-reconcile",
        WorkflowType.ORG_MACHINE_RECONCILE,
        every=timedelta(seconds=30),
        catchup_window=CATCHUP_TIGHT,
    ),
    # A workspace move whose workflow went with Temporal's history is offered
    # its runner again; a move whose runner is alive is left alone.
    ScheduleEntry(
        "recover-machine-moves",
        WorkflowType.WORKSPACE_MACHINE_MOVE_RECOVER,
        every=timedelta(minutes=1),
        catchup_window=CATCHUP_TIGHT,
        execution_timeout=timedelta(minutes=10),
    ),
    # Each configured provider's live price + stock, reconciled onto its own
    # catalog rows. Hourly: prices move slowly and the refresh never re-rates a
    # running machine (the meter bills pinned prices), so it is not a money-tick;
    # a run missed during an outage is still worth taking any time in its hour.
    ScheduleEntry(
        "compute-catalog",
        WorkflowType.COMPUTE_CATALOG,
        cron="15 * * * *",
        catchup_window=CATCHUP_HOURLY,
    ),
    # A chat warmed ahead of its owner's first message is reaped once the page
    # stops beating (three minutes) or after two hours regardless; a minute
    # between passes is well inside both, and a pass with nothing to reap is
    # one indexed read.
    ScheduleEntry(
        "reap-chat-spares",
        WorkflowType.REAP_CHAT_SPARES,
        every=timedelta(minutes=1),
        catchup_window=CATCHUP_TIGHT,
    ),
    # A chat a previous build left with no workspace is adopted, and a
    # workspace of one whose chat is gone is retired. Reads heal a chat someone
    # opens; this catches the ones nobody does, within a quarter hour.
    ScheduleEntry(
        "reconcile-workspaces",
        WorkflowType.RECONCILE_WORKSPACES,
        every=timedelta(minutes=15),
        catchup_window=CATCHUP_TIGHT,
    ),
    # FALLBACK only: the delete route nudges the drain the moment a workspace
    # with chats is deleted; this tick covers a lost nudge or a worker that was
    # down. A pass with nothing to finish is one read of an empty index.
    ScheduleEntry(
        "finish-workspace-deletions",
        WorkflowType.FINISH_WORKSPACE_DELETIONS,
        every=timedelta(minutes=2),
        catchup_window=CATCHUP_TIGHT,
    ),
    # Account deletions whose grace window ended are erased. A pass with
    # nothing due is one indexed read.
    ScheduleEntry(
        "account-lifecycle-sweep",
        WorkflowType.ACCOUNT_LIFECYCLE_SWEEP,
        every=timedelta(minutes=15),
        catchup_window=CATCHUP_TIGHT,
    ),
    # One Files reconciliation pass runs every sweeper in JANITOR_ORDER — the
    # dir-stats aggregation, the lease reaper and the collapse of the versions
    # a mounted agent's autosaves leave behind among them — so nothing in that
    # ledger needs a schedule of its own and there is one place to look when
    # something lingers. Five minutes is well inside the tightest deadline any
    # sweeper enforces (a lease reaped, a stuck `committing` session re-driven
    # within the hour, a cold generation of live writes collapsed ten minutes
    # after it ended), and every sweeper is budgeted, so a pass with a backlog
    # leaves a cursor and the next tick resumes from it rather than running
    # long. SKIP, like every entry: a tick that fires while the previous pass
    # is still going is dropped, never stacked.
    ScheduleEntry(
        "files-janitor",
        WorkflowType.FILES_JANITOR,
        every=timedelta(minutes=5),
        catchup_window=CATCHUP_TIGHT,
    ),
    # The bytes the janitor structurally cannot see: a dedup domain no drive
    # row names any more, swept by no org's pass because no org has it. Daily
    # rather than five-minutely because nothing waits on it -- a collected
    # object is a week from being erased either way -- and because the pass
    # lists the whole bucket, which is worth doing once a day and not 288
    # times. 04:10 UTC keeps it off the hour every other job shares.
    # The net under every Files hand-off: a route nudges the workflow that runs
    # its queued operation inside a two-second budget, and a briefly unreachable
    # orchestrator -- or a process killed after the 202 -- leaves a durable row
    # nobody was ever told about. Nothing else looks at `queued` rows (the
    # watchdog only judges `running` ones by their heartbeat), so a lost nudge
    # strands a bulk trash, a copy or an oversized move forever. Thirty seconds
    # because the operation a person is watching is what waits on it, and
    # because it matches the staleness threshold a row must cross first; an idle
    # fleet costs one indexed read per tick.
    ScheduleEntry(
        "files-recover-queued",
        WorkflowType.FILES_RECOVER_QUEUED,
        every=timedelta(seconds=30),
        catchup_window=CATCHUP_TIGHT,
        # A pass that runs twice a minute cannot be allowed the default hour: a
        # run that wedges would hold the id long past the tick that replaces it.
        execution_timeout=timedelta(minutes=10),
    ),
    ScheduleEntry(
        "files-gc",
        WorkflowType.FILES_GC,
        cron="10 4 * * *",
        catchup_window=CATCHUP_DAILY,
        execution_timeout=timedelta(hours=6),
    ),
)
"""The platform's recurring jobs, keyed by their long-standing ids."""

SCHEDULE_SETS: ExtensionPoint[tuple[ScheduleEntry, ...]] = ExtensionPoint("worker_schedules")
"""The schedules a distribution's job families bring, registered beside their
job modules. With nothing registered the catalog is the platform's own."""


def merge_schedules(
    own: Sequence[ScheduleEntry], registered: Sequence[Sequence[ScheduleEntry]]
) -> tuple[ScheduleEntry, ...]:
    """``own`` followed by every registered set. An id declared twice is a
    wiring bug, so it raises instead of letting one entry shadow the other."""
    entries = [*own, *(entry for entries in registered for entry in entries)]
    ids = [entry.id for entry in entries]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise ValueError(f"schedules declared twice: {twice}")
    return tuple(entries)


@cache
def catalog() -> tuple[ScheduleEntry, ...]:
    """Every recurring job: the platform's and every registered family's.
    Reading it closes :data:`SCHEDULE_SETS`."""
    return merge_schedules(_OPEN_SCHEDULES, SCHEDULE_SETS.items())


# Declared, not assigned: the module ``__getattr__`` below builds it on first
# read, after composition has registered every family's schedules.
SCHEDULES: tuple[ScheduleEntry, ...]
"""The catalog, as :func:`catalog` builds it."""


def __getattr__(name: str) -> object:
    if name == "SCHEDULES":
        return catalog()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _range_key(r: ScheduleRange) -> tuple[int, int, int]:
    end = r.end if r.end >= r.start else r.start
    return (r.start, end, r.step or 1)


def _calendar_key(
    cal: ScheduleCalendarSpec,
) -> tuple[tuple[str, tuple[tuple[int, int, int], ...]], ...]:
    return tuple(
        (name, tuple(_range_key(r) for r in getattr(cal, name)))
        for name in ("second", "minute", "hour", "day_of_month", "month", "year", "day_of_week")
    )


def fingerprint(schedule: Schedule) -> dict[str, Any]:
    """The server-normalised identity of a schedule: what a spec change changes,
    nothing a describe() fills in on its own (timestamps, counters), and nothing
    an operator owns at the console — the pause, the note, an action cap. The
    catalog never emits any of those, so reading them as drift would have every
    worker boot undo an operator's pause."""
    action = schedule.action
    if not isinstance(action, ScheduleActionStartWorkflow):
        raise TypeError(f"unsupported schedule action {type(action).__name__}")
    workflow_name = action.workflow if isinstance(action.workflow, str) else None
    if workflow_name is None:
        from worker.temporal.queues import workflow_type_name

        workflow_name = workflow_type_name(action.workflow)
    spec = schedule.spec
    calendars = [_calendar_key(c) for c in spec.calendars]
    calendars += [_calendar_key(calendar_from_cron(c)) for c in spec.cron_expressions]
    intervals = [
        (i.every.total_seconds(), i.offset.total_seconds() if i.offset else 0.0)
        for i in spec.intervals
    ]
    return {
        "workflow": workflow_name,
        "workflow_id": action.id,
        "task_queue": action.task_queue,
        "execution_timeout_s": (
            action.execution_timeout.total_seconds() if action.execution_timeout else None
        ),
        "calendars": sorted(calendars),
        "intervals": sorted(intervals),
        "time_zone": spec.time_zone_name,
        "overlap": schedule.policy.overlap.name,
        "catchup_s": schedule.policy.catchup_window.total_seconds(),
        "pause_on_failure": schedule.policy.pause_on_failure,
    }


# --- the reconciler ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleSyncReport:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    """Catalog entries whose workflow no worker serves yet; not created."""
    foreign: tuple[str, ...] = ()
    """Catalog ids already taken by a schedule someone else owns; left untouched."""

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "created": list(self.created),
            "updated": list(self.updated),
            "deleted": list(self.deleted),
            "unchanged": list(self.unchanged),
            "pending": list(self.pending),
            "foreign": list(self.foreign),
        }


@dataclass
class _Tally:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)

    def report(self) -> ScheduleSyncReport:
        return ScheduleSyncReport(
            created=tuple(self.created),
            updated=tuple(self.updated),
            deleted=tuple(self.deleted),
            unchanged=tuple(self.unchanged),
            pending=tuple(self.pending),
            foreign=tuple(self.foreign),
        )


#: Workflows that need the Files object store wired before they can run. The
#: registry is a module walk, so every one of these is "registered" in every
#: deployment — including one with FILES_ENABLED=false, where `wire_files_jobs`
#: never installs a store factory and the activity raises. Without this the
#: daily `files.gc` schedule would be created on a non-Files deployment and burn
#: its whole retry budget every night, on the one signal (the failed-workflow
#: count) an operator would otherwise trust.
_FILES_WORKFLOWS: frozenset[WorkflowType] = frozenset(
    {
        WorkflowType.FILES_PROMOTE,
        WorkflowType.FILES_JANITOR,
        WorkflowType.FILES_GC,
        WorkflowType.FILES_ACL_REWRITE,
        WorkflowType.FILES_LARGE_MOVE,
        WorkflowType.FILES_COPY,
        WorkflowType.FILES_BULK,
        WorkflowType.FILES_RECOVER_QUEUED,
    }
)


def _default_servable(workflow: StrEnum) -> bool:
    from alkera_core.config import get_settings

    from worker.temporal.queues import served_workflow_types

    if workflow in _FILES_WORKFLOWS and not settings.files_enabled:
        return False
    # The collection pass is opt-in on top of that: a deployment that has not
    # turned it on gets no schedule for it rather than one that fires daily to
    # say so. An existing schedule is left alone (pending, like any unserved
    # entry), and the job itself answers ``disabled`` cleanly.
    if workflow is WorkflowType.FILES_GC and not get_settings().files_gc_active:
        return False
    return workflow.value in served_workflow_types()


async def _managed_ids(client: Client, managed_by: str) -> set[str]:
    """Ids of every schedule stamped with our memo. The list entry carries the
    raw memo payload; it is decoded with the client's own converter."""
    ids: set[str] = set()
    async for entry in await client.list_schedules():
        payload = entry.raw_entry.memo.fields.get(MANAGED_MEMO_KEY)
        if payload is None:
            continue
        [owner] = await client.data_converter.decode([payload], [str])
        if owner == managed_by:
            ids.add(entry.id)
    return ids


async def _reconcile_existing(
    client: Client,
    entry: ScheduleEntry,
    desired: Schedule,
    *,
    dry_run: bool,
    tally: _Tally,
    require_owner: str | None = None,
) -> None:
    """Converge one existing schedule on ``desired``.

    ``require_owner`` is set on the path that reached a schedule without having
    listed it as ours: the id is in the catalog, but the schedule under it may be
    an operator's own or another deployment's in a shared namespace. Its memo is
    the only proof of ownership, so it is read before anything is written and a
    schedule stamped by someone else is left exactly as it is.
    """
    handle = client.get_schedule_handle(entry.id)
    described = await handle.describe()
    if require_owner is not None:
        owner = await described.memo_value(MANAGED_MEMO_KEY, None, type_hint=str)
        if owner != require_owner:
            log.warning(
                "worker.schedule_foreign_id",
                schedule_id=entry.id,
                managed_by=owner,
                expected=require_owner,
            )
            tally.foreign.append(entry.id)
            return
    if fingerprint(described.schedule) == fingerprint(desired):
        tally.unchanged.append(entry.id)
        return
    if not dry_run:
        # The update replaces the whole schedule, so the state the operator set
        # at the console (a pause and its note, an action cap) is carried over
        # from the description the update sees; the catalog only owns the spec.
        await handle.update(
            lambda update: ScheduleUpdate(
                schedule=dataclasses.replace(desired, state=update.description.schedule.state)
            )
        )
    tally.updated.append(entry.id)


async def sync_schedules(
    client: Client,
    desired: Sequence[ScheduleEntry] | None = None,
    *,
    dry_run: bool = False,
    managed_by: str = MANAGED_BY,
    servable: Callable[[StrEnum], bool] | None = None,
) -> ScheduleSyncReport:
    """Make the namespace's managed schedules match ``desired``.

    ``managed_by`` is the ownership marker (the memo value); only schedules
    stamped with it are compared or deleted, so the test suite reconciles
    throwaway catalogs under its own marker on a shared dev server without
    touching anyone else's. ``servable`` decides whether an entry's workflow can
    run today (default: it is registered under ``worker.workflows``); entries
    that cannot are reported ``pending`` and left alone. ``dry_run`` reports
    what would change and writes nothing.
    """
    is_servable = servable or _default_servable
    if desired is None:
        desired = catalog()
    tally = _Tally()
    existing = await _managed_ids(client, managed_by)
    memo = {MANAGED_MEMO_KEY: managed_by}
    wanted_ids: set[str] = set()

    for entry in desired:
        wanted_ids.add(entry.id)
        if not is_servable(entry.workflow):
            tally.pending.append(entry.id)
            continue
        desired_schedule = entry.to_schedule()
        if entry.id in existing:
            await _reconcile_existing(client, entry, desired_schedule, dry_run=dry_run, tally=tally)
            continue
        if dry_run:
            tally.created.append(entry.id)
            continue
        try:
            await client.create_schedule(entry.id, desired_schedule, memo=memo)
        except ScheduleAlreadyRunningError:
            # Something already holds this id. Usually another default-queue worker
            # booting at the same time (or a list that had not caught up with a fresh
            # create), in which case the memo says it is ours and we converge on what
            # exists -- but the id could equally belong to an operator's hand-made
            # schedule or to another deployment sharing the namespace, and those are
            # never written to.
            await _reconcile_existing(
                client,
                entry,
                desired_schedule,
                dry_run=dry_run,
                tally=tally,
                require_owner=managed_by,
            )
            continue
        tally.created.append(entry.id)

    for stale in sorted(existing - wanted_ids):
        if not dry_run:
            await client.get_schedule_handle(stale).delete()
        tally.deleted.append(stale)

    report = tally.report()
    log.info(
        "worker.schedules_synced",
        dry_run=dry_run,
        created=len(report.created),
        updated=len(report.updated),
        deleted=len(report.deleted),
        unchanged=len(report.unchanged),
        pending=list(report.pending),
        foreign=list(report.foreign),
    )
    return report
