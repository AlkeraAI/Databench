"""The schedule catalog (data) and the reconciler (against the dev server).

The catalog is pinned against the legacy beat table literally — id, spec,
queue, catch-up window — so a drifted entry fails here, not in production. The
reconciler cases run on the real dev server under a per-test ownership marker
so they share one server without touching each other; the last case observes
``overlap=SKIP`` doing its job.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.temporal import QUEUE_FOR, WorkflowType
from temporalio import workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
)
from worker import schedules as schedules_mod
from worker.schedules import (
    MANAGED_BY,
    MANAGED_MEMO_KEY,
    MANAGED_NOTE,
    SCHEDULES,
    ScheduleEntry,
    ScheduleSyncReport,
    calendar_from_cron,
    fingerprint,
    merge_schedules,
    sync_schedules,
)

# --- the catalog, pinned against the legacy beat table -------------------------

LEGACY_BEAT: dict[str, tuple[str, str, str, timedelta]] = {
    # id: (workflow type, spec, queue, catch-up window)
    "prune-expired-tokens": (
        "auth.prune_expired_tokens",
        "0 3 * * *",
        "default",
        timedelta(hours=24),
    ),
    "prune-expired-device-codes": (
        "auth.prune_expired_device_codes",
        "15 3 * * *",
        "default",
        timedelta(hours=24),
    ),
    "entitlements-watchdog": ("entitlements.watchdog", "0 1 * * *", "default", timedelta(hours=24)),
    "deployment-health": ("deployment_health.run", "*/5 * * * *", "default", timedelta(minutes=1)),
    "compute-meter": ("compute.meter", "every 60s", "money", timedelta(minutes=1)),
    "compute-sweep": ("compute.sweep", "every 15s", "default", timedelta(minutes=1)),
}

#: The platform's own entries; a registered family pins its rows beside its code.
OPEN = tuple(entry for entry in SCHEDULES if type(entry.workflow) is WorkflowType)
BY_ID = {entry.id: entry for entry in OPEN}


# The platform's jobs with no beat-era line.
POST_BEAT = {
    "compute-reconcile",
    "compute-org-machine-reconcile",
    "recover-machine-moves",
    "compute-catalog",
    "files-janitor",
    "files-gc",
    "reap-chat-spares",
    "reconcile-workspaces",
    "finish-workspace-deletions",
    "prune-login-lockouts",
    "files-recover-queued",
    "account-lifecycle-sweep",
    "prune-identity-security-events",
    "sweep-notebook-runs",
}


def test_the_catalog_has_exactly_the_beat_entries_plus_what_came_after() -> None:
    assert len(OPEN) == 20
    assert len(BY_ID) == 20, "ids are unique"
    assert set(BY_ID) == set(LEGACY_BEAT) | POST_BEAT


@pytest.mark.parametrize("schedule_id", sorted(LEGACY_BEAT))
def test_each_entry_matches_its_legacy_beat_line(schedule_id: str) -> None:
    workflow_type, spec, queue, catchup = LEGACY_BEAT[schedule_id]
    entry = BY_ID[schedule_id]
    assert entry.workflow.value == workflow_type
    assert entry.spec_text == spec
    assert entry.queue.value == queue
    assert entry.catchup_window == catchup


@pytest.mark.parametrize("entry", SCHEDULES, ids=lambda e: e.id)
def test_each_entry_builds_a_skip_overlap_schedule_on_its_queue(entry: ScheduleEntry) -> None:
    schedule = entry.to_schedule()
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == entry.workflow.value
    assert action.id == entry.id
    assert action.task_queue == QUEUE_FOR[entry.workflow].value
    assert action.execution_timeout == entry.execution_timeout
    assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
    assert schedule.policy.pause_on_failure is False
    assert schedule.policy.catchup_window == entry.catchup_window
    assert schedule.state.note == MANAGED_NOTE
    assert schedule.state.paused is False
    if entry.cron is not None:
        assert schedule.spec.cron_expressions == [entry.cron] and not schedule.spec.intervals
    else:
        assert schedule.spec.intervals == [ScheduleIntervalSpec(every=entry.every or timedelta())]
        assert not schedule.spec.cron_expressions


def test_only_four_schedules_depart_from_the_hour_long_run_budget() -> None:
    """The hour is the budget a run gets before the server cuts it off, and it
    suits every job that fires at most once an hour and finishes in one go.

    Four do not. The verification recovery fires twice a minute, so an hour of
    it would be a wedged run holding the schedule's id long after the dialog
    waiting on it gave up — it gets five minutes. The re-validation pass visits
    every connection whose last look is older than the cadence, continuing as
    new for as many activity-sized attempts as that takes, so its budget has to
    span the whole chain rather than one attempt; six hours is a
    non-convergence bound, well inside the cadence the pass exists to meet. The
    Files collection walks every domain prefix in the bucket a page at a time,
    so its run is as long as the bucket is wide; it gets the same six hours,
    which is a bound rather than an expectation and is still a quarter of its
    daily cadence. The Files queued-operation recovery fires twice a minute for
    the same reason the verification one does — an abandoned operation is one a
    person is watching — so an hour would let a wedged run hold its id through
    twenty ticks of the net that was meant to replace it. All four are spelled
    here, where dropping an override would be caught."""
    overrides = {
        e.id: e.execution_timeout for e in OPEN if e.execution_timeout != timedelta(hours=1)
    }

    assert overrides == {
        "files-gc": timedelta(hours=6),
        "files-recover-queued": timedelta(minutes=10),
        "recover-machine-moves": timedelta(minutes=10),
    }


def test_catchup_windows_follow_the_cadence() -> None:
    for entry in SCHEDULES:
        if entry.every is not None or entry.cron == "*/5 * * * *":
            assert entry.catchup_window == timedelta(minutes=1), entry.id
        elif entry.cron is not None and entry.cron.split()[1] == "*":
            assert entry.catchup_window == timedelta(hours=1), entry.id
        else:
            assert entry.catchup_window == timedelta(hours=24), entry.id


def test_every_workflow_type_that_beat_ran_is_scheduled_at_most_once() -> None:
    scheduled = [e.workflow for e in SCHEDULES]
    assert len(scheduled) == len(set(scheduled))
    # The only platform workflow types without a schedule are the nudge-only ones.
    unscheduled = set(WorkflowType) - set(scheduled)
    assert unscheduled == {
        # Started per upload, per subtree, per queued move, per copy and per
        # over-threshold batch — never on a clock.
        WorkflowType.FILES_PROMOTE,
        WorkflowType.FILES_ACL_REWRITE,
        WorkflowType.FILES_LARGE_MOVE,
        WorkflowType.FILES_COPY,
        WorkflowType.FILES_BULK,
        # Started per move a person asks for, and re-armed by whoever next
        # reads a move whose run is gone.
        WorkflowType.WORKSPACE_MACHINE_MOVE,
        # Started on every worker boot and by the restore runbook, never on a clock.
        WorkflowType.ACCOUNT_REERASE,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="neither"),
        pytest.param({"cron": "1 * * * *", "every": timedelta(seconds=5)}, id="both"),
        pytest.param({"cron": "1 * * *"}, id="four-field-cron"),
        pytest.param({"cron": "60 * * * *"}, id="minute-out-of-range"),
        pytest.param({"every": timedelta(0)}, id="zero-interval"),
        pytest.param({"every": timedelta(seconds=-1)}, id="negative-interval"),
    ],
)
def test_an_invalid_entry_is_refused_at_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ScheduleEntry("bad", WorkflowType.FILES_GC, **kwargs)


# --- cron normalisation ----------------------------------------------------------


def _ranges(spec: ScheduleCalendarSpec, name: str) -> list[tuple[int, int, int]]:
    return [(r.start, r.end, r.step) for r in getattr(spec, name)]


@pytest.mark.parametrize(
    ("cron", "minute", "hour"),
    [
        pytest.param("10 * * * *", (10, 10, 1), (0, 23, 1), id="hourly-at-10"),
        pytest.param("30 2 * * *", (30, 30, 1), (2, 2, 1), id="daily-02-30"),
        pytest.param("*/5 * * * *", (0, 59, 5), (0, 23, 1), id="every-five-minutes"),
        pytest.param("0 3 * * *", (0, 0, 1), (3, 3, 1), id="daily-03-00"),
    ],
)
def test_calendar_from_cron_matches_the_servers_normalised_form(
    cron: str, minute: tuple[int, int, int], hour: tuple[int, int, int]
) -> None:
    """The exact ranges the dev server describes back for these expressions
    (verified against it; the reconciler test below re-checks every catalog entry)."""
    cal = calendar_from_cron(cron)
    assert _ranges(cal, "second") == [(0, 0, 1)]
    assert _ranges(cal, "minute") == [minute]
    assert _ranges(cal, "hour") == [hour]
    assert _ranges(cal, "day_of_month") == [(1, 31, 1)]
    assert _ranges(cal, "month") == [(1, 12, 1)]
    assert _ranges(cal, "year") == []
    assert _ranges(cal, "day_of_week") == [(0, 6, 1)]


@pytest.mark.parametrize(
    "cron",
    [
        pytest.param("* * * *", id="four-fields"),
        pytest.param("* * * * * *", id="six-fields"),
        pytest.param("1-5 * * * *", id="range"),
        pytest.param("1,2 * * * *", id="list"),
        pytest.param("0 0 * * mon", id="name"),
        pytest.param("60 * * * *", id="minute-60"),
        pytest.param("0 24 * * *", id="hour-24"),
        pytest.param("0 0 0 * *", id="day-0"),
        pytest.param("*/0 * * * *", id="zero-step"),
        pytest.param("@hourly", id="macro"),
    ],
)
def test_calendar_from_cron_refuses_grammar_the_catalog_does_not_use(cron: str) -> None:
    with pytest.raises(ValueError):
        calendar_from_cron(cron)


# --- fingerprint ---------------------------------------------------------------------


def _desired(**overrides: Any) -> Schedule:
    kwargs: dict[str, Any] = {"cron": "30 2 * * *", **overrides}
    return ScheduleEntry("fp", WorkflowType.FILES_GC, **kwargs).to_schedule()


def _as_described(schedule: Schedule, *, note: str | None = "server rewrote me") -> Schedule:
    """What describe() returns for a cron schedule: calendars, no cron string, and
    fields the server fills in on its own."""
    spec = schedule.spec
    return Schedule(
        action=schedule.action,
        spec=ScheduleSpec(
            calendars=[calendar_from_cron(c) for c in spec.cron_expressions],
            intervals=list(spec.intervals),
        ),
        policy=schedule.policy,
        state=ScheduleState(note=note, paused=schedule.state.paused),
    )


def test_fingerprint_equates_a_cron_with_its_described_calendar_form() -> None:
    assert fingerprint(_desired()) == fingerprint(_as_described(_desired()))


def test_fingerprint_ignores_the_note() -> None:
    assert fingerprint(_as_described(_desired(), note=None)) == fingerprint(_desired())


def test_fingerprint_normalises_range_defaults() -> None:
    """A hand-built ``ScheduleRange(start=10)`` (end 0, step 0) means 10..10 step 1."""
    loose = Schedule(
        action=_desired().action,
        spec=ScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(
                    second=(ScheduleRange(start=0),),
                    minute=(ScheduleRange(start=30),),
                    hour=(ScheduleRange(start=2),),
                    day_of_month=(ScheduleRange(start=1, end=31),),
                    month=(ScheduleRange(start=1, end=12),),
                    year=(),
                    day_of_week=(ScheduleRange(start=0, end=6),),
                )
            ]
        ),
        policy=_desired().policy,
        state=_desired().state,
    )
    assert fingerprint(loose) == fingerprint(_desired())


@pytest.mark.parametrize(
    ("changed", "field"),
    [
        pytest.param(_desired(cron="45 2 * * *"), "calendars", id="cron"),
        pytest.param(_desired(catchup_window=timedelta(hours=2)), "catchup_s", id="catchup"),
        pytest.param(
            _desired(execution_timeout=timedelta(hours=2)), "execution_timeout_s", id="timeout"
        ),
    ],
)
def test_fingerprint_changes_with_every_operator_visible_field(
    changed: Schedule, field: str
) -> None:
    base = fingerprint(_desired())
    other = fingerprint(changed)
    assert other != base
    assert other[field] != base[field]


def test_fingerprint_tracks_queue_and_overlap() -> None:
    base = _desired()
    requeued = Schedule(
        action=ScheduleActionStartWorkflow(
            "files.gc",
            id="fp",
            task_queue="sync",
            execution_timeout=timedelta(hours=1),
        ),
        spec=base.spec,
        policy=base.policy,
        state=base.state,
    )
    assert fingerprint(requeued)["task_queue"] == "sync" != fingerprint(base)["task_queue"]
    buffered = Schedule(
        action=base.action,
        spec=base.spec,
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.BUFFER_ONE, catchup_window=timedelta(hours=24)
        ),
        state=base.state,
    )
    assert fingerprint(buffered)["overlap"] == "BUFFER_ONE"


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(ScheduleState(paused=True), id="paused"),
        pytest.param(ScheduleState(paused=True, note="on hold: incident 4711"), id="paused-noted"),
        pytest.param(ScheduleState(note="operator remark"), id="noted"),
        pytest.param(
            ScheduleState(paused=True, limited_actions=True, remaining_actions=3), id="limited"
        ),
    ],
)
def test_fingerprint_ignores_the_operator_owned_state(state: ScheduleState) -> None:
    """Pausing a schedule, noting it, or capping its actions is the operator's
    call at the console; the catalog describes the spec and never emits any of
    it, so none of it may read as drift — or every worker boot would undo it."""
    base = _desired()
    operated = Schedule(action=base.action, spec=base.spec, policy=base.policy, state=state)
    assert fingerprint(operated) == fingerprint(base)
    assert "paused" not in fingerprint(operated)


def test_fingerprint_of_an_interval_schedule() -> None:
    entry = ScheduleEntry("iv", WorkflowType.FILES_RECOVER_QUEUED, every=timedelta(seconds=30))
    fp = fingerprint(entry.to_schedule())
    assert fp["intervals"] == [(30.0, 0.0)] and fp["calendars"] == []


def test_sync_report_as_dict_lists_every_bucket() -> None:
    report = ScheduleSyncReport(created=("a",), pending=("b", "c"))
    assert report.as_dict() == {
        "created": ["a"],
        "updated": [],
        "deleted": [],
        "unchanged": [],
        "pending": ["b", "c"],
        "foreign": [],
    }


# --- the reconciler, against the dev server -----------------------------------------


@pytest.fixture
def owner() -> str:
    """A per-test ownership marker so parallel tests share one server safely."""
    return f"test-{uuid4().hex[:10]}"


@pytest.fixture
def catalog(owner: str) -> tuple[ScheduleEntry, ScheduleEntry]:
    return (
        ScheduleEntry(
            f"{owner}-a",
            WorkflowType.FILES_GC,
            cron="30 2 * * *",
            catchup_window=timedelta(hours=24),
        ),
        ScheduleEntry(
            f"{owner}-b",
            WorkflowType.FILES_RECOVER_QUEUED,
            every=timedelta(seconds=30),
            catchup_window=timedelta(minutes=1),
        ),
    )


def _all_servable(_: WorkflowType) -> bool:
    return True


async def _sync(client: Client, desired: Any, owner: str, **kwargs: Any) -> ScheduleSyncReport:
    return await sync_schedules(client, desired, managed_by=owner, servable=_all_servable, **kwargs)


async def _wait_listed(client: Client, owner: str, expected: set[str]) -> None:
    """Visibility on the dev server is eventually consistent; wait for the list."""
    for _ in range(200):
        if await schedules_mod._managed_ids(client, owner) == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"managed ids never became {expected}")


async def _describe_or_none(client: Client, schedule_id: str) -> Any:
    try:
        return await client.get_schedule_handle(schedule_id).describe()
    except Exception:
        return None


async def _cleanup(client: Client, ids: list[str]) -> None:
    for schedule_id in ids:
        try:
            await client.get_schedule_handle(schedule_id).delete()
        except Exception:
            pass


@pytest.mark.temporal
async def test_first_sync_creates_every_entry_with_the_ownership_memo(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    ids = [e.id for e in catalog]
    try:
        report = await _sync(temporal_client, catalog, owner)
        assert report == ScheduleSyncReport(created=tuple(ids))
        for entry in catalog:
            described = await temporal_client.get_schedule_handle(entry.id).describe()
            assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
            [memo_owner] = await temporal_client.data_converter.decode(
                [described.raw_description.memo.fields[MANAGED_MEMO_KEY]], [str]
            )
            assert memo_owner == owner
            assert described.schedule.state.note == MANAGED_NOTE
        await _wait_listed(temporal_client, owner, set(ids))
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_a_second_sync_changes_nothing(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    ids = [e.id for e in catalog]
    try:
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        report = await _sync(temporal_client, catalog, owner)
        assert report == ScheduleSyncReport(unchanged=tuple(ids))
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_a_spec_change_is_applied_in_place(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    a, b = catalog
    ids = [a.id, b.id]
    try:
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        moved = ScheduleEntry(a.id, a.workflow, cron="45 2 * * *", catchup_window=a.catchup_window)
        report = await _sync(temporal_client, (moved, b), owner)
        assert report == ScheduleSyncReport(updated=(a.id,), unchanged=(b.id,))
        described = await temporal_client.get_schedule_handle(a.id).describe()
        assert fingerprint(described.schedule) == fingerprint(moved.to_schedule())
        assert _ranges(described.schedule.spec.calendars[0], "minute") == [(45, 45, 1)]
        report = await _sync(temporal_client, (moved, b), owner)
        assert report == ScheduleSyncReport(unchanged=(a.id, b.id))
    finally:
        await _cleanup(temporal_client, ids)


_OPERATOR_NOTE = "on hold: incident 4711"


@pytest.mark.temporal
async def test_an_operator_pause_survives_a_sync_that_changes_nothing(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    """A default-queue worker boots (or `schedules sync` runs) while an operator
    has paused a schedule for an incident. The pause and its note are the
    operator's, not the catalog's: the sync reports no drift and lifts nothing."""
    a, b = catalog
    ids = [a.id, b.id]
    try:
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        handle = temporal_client.get_schedule_handle(a.id)
        await handle.pause(note=_OPERATOR_NOTE)

        report = await _sync(temporal_client, catalog, owner)

        assert report == ScheduleSyncReport(unchanged=(a.id, b.id))
        state = (await handle.describe()).schedule.state
        assert state.paused is True
        assert state.note == _OPERATOR_NOTE
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_a_spec_change_lands_without_lifting_an_operator_pause(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    """The catalog moved while the schedule was paused: the new spec is applied
    in place, the pause and the operator's note ride along, and only an
    operator's unpause (with its own note) resumes it — the next sync keeps
    that too."""
    a, b = catalog
    ids = [a.id, b.id]
    try:
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        handle = temporal_client.get_schedule_handle(a.id)
        await handle.pause(note=_OPERATOR_NOTE)
        moved = ScheduleEntry(a.id, a.workflow, cron="45 2 * * *", catchup_window=a.catchup_window)

        report = await _sync(temporal_client, (moved, b), owner)

        assert report == ScheduleSyncReport(updated=(a.id,), unchanged=(b.id,))
        described = await handle.describe()
        assert fingerprint(described.schedule) == fingerprint(moved.to_schedule())
        assert _ranges(described.schedule.spec.calendars[0], "minute") == [(45, 45, 1)]
        assert described.schedule.state.paused is True
        assert described.schedule.state.note == _OPERATOR_NOTE
        assert await _sync(temporal_client, (moved, b), owner) == ScheduleSyncReport(
            unchanged=(a.id, b.id)
        )
        assert (await handle.describe()).schedule.state.paused is True

        await handle.unpause(note="resolved")
        assert await _sync(temporal_client, (moved, b), owner) == ScheduleSyncReport(
            unchanged=(a.id, b.id)
        )
        state = (await handle.describe()).schedule.state
        assert state.paused is False and state.note == "resolved"
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_an_entry_dropped_from_the_catalog_is_deleted(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    a, b = catalog
    ids = [a.id, b.id]
    try:
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        report = await _sync(temporal_client, (b,), owner)
        assert report == ScheduleSyncReport(deleted=(a.id,), unchanged=(b.id,))
        assert await _describe_or_none(temporal_client, a.id) is None
        assert await _describe_or_none(temporal_client, b.id) is not None
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_schedules_the_reconciler_does_not_own_are_never_touched(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    a, b = catalog
    unmarked = f"{owner}-unmarked"
    other_owner = f"{owner}-other"
    foreign = f"{owner}-foreign"
    ids = [a.id, b.id, unmarked, foreign]
    try:
        stray = ScheduleEntry(unmarked, WorkflowType.FILES_JANITOR, cron="5 * * * *")
        await temporal_client.create_schedule(unmarked, stray.to_schedule())
        stray2 = ScheduleEntry(foreign, WorkflowType.FILES_JANITOR, cron="5 * * * *")
        await temporal_client.create_schedule(
            foreign, stray2.to_schedule(), memo={MANAGED_MEMO_KEY: other_owner}
        )
        await _wait_listed(temporal_client, other_owner, {foreign})
        report = await _sync(temporal_client, catalog, owner)
        assert report.deleted == ()
        assert set(report.created) == {a.id, b.id}
        assert await _describe_or_none(temporal_client, unmarked) is not None
        assert await _describe_or_none(temporal_client, foreign) is not None
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
@pytest.mark.parametrize(
    "squatter_memo",
    [
        pytest.param({MANAGED_MEMO_KEY: "another-deployment"}, id="another-deployment"),
        pytest.param(None, id="an-operators-own"),
    ],
)
async def test_a_catalog_id_already_taken_is_reported_not_overwritten(
    temporal_client: Client,
    owner: str,
    catalog: tuple[ScheduleEntry, ScheduleEntry],
    squatter_memo: dict[str, str] | None,
) -> None:
    """Ownership is the memo, on the create path too.

    A schedule can carry an id from our catalog without being ours -- an operator
    made one by hand, or another deployment shares the namespace. The reconciler
    does not see it in its own list, so it tries to create and is told the id is
    taken; converging there would silently rewrite someone else's spec. It must
    read the memo, leave the schedule exactly as it found it, and say so.
    """
    a, _ = catalog
    squatter = ScheduleEntry(a.id, a.workflow, cron="7 4 * * *", catchup_window=a.catchup_window)
    before = squatter.to_schedule()
    assert fingerprint(before) != fingerprint(a.to_schedule()), "the specs must differ"
    try:
        await temporal_client.create_schedule(a.id, before, memo=squatter_memo)
        report = await _sync(temporal_client, (a,), owner)
        assert report == ScheduleSyncReport(foreign=(a.id,))
        described = await temporal_client.get_schedule_handle(a.id).describe()
        assert fingerprint(described.schedule) == fingerprint(before), "someone else's spec changed"
        memo = await described.memo_value(MANAGED_MEMO_KEY, None, type_hint=str)
        assert memo == (squatter_memo or {}).get(MANAGED_MEMO_KEY), "the memo was restamped"
    finally:
        await _cleanup(temporal_client, [a.id])


@pytest.mark.temporal
async def test_our_own_schedule_still_converges_when_the_list_lagged(
    temporal_client: Client,
    owner: str,
    catalog: tuple[ScheduleEntry, ScheduleEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ownership check must not cost the case it shares a branch with: a
    schedule that IS ours, met through the create race because the list had not
    caught up, still takes the catalog's spec."""
    a, _ = catalog
    moved = ScheduleEntry(a.id, a.workflow, cron="45 2 * * *", catchup_window=a.catchup_window)
    try:
        await _sync(temporal_client, (a,), owner)
        await _wait_listed(temporal_client, owner, {a.id})

        async def lagging_list(client: Any, managed_by: str) -> set[str]:
            return set()

        monkeypatch.setattr(schedules_mod, "_managed_ids", lagging_list)
        report = await _sync(temporal_client, (moved,), owner)
        assert report == ScheduleSyncReport(updated=(a.id,))
        described = await temporal_client.get_schedule_handle(a.id).describe()
        assert fingerprint(described.schedule) == fingerprint(moved.to_schedule())
    finally:
        await _cleanup(temporal_client, [a.id])


@pytest.mark.temporal
async def test_entries_whose_workflow_no_worker_serves_are_pending_not_created(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    a, b = catalog
    ids = [a.id, b.id]
    try:
        report = await sync_schedules(
            temporal_client,
            catalog,
            managed_by=owner,
            servable=lambda wf: wf is WorkflowType.FILES_GC,
        )
        assert report == ScheduleSyncReport(created=(a.id,), pending=(b.id,))
        assert await _describe_or_none(temporal_client, b.id) is None
        await _wait_listed(temporal_client, owner, {a.id})
        # Becoming servable later creates it; a still-pending one is never deleted.
        report = await _sync(temporal_client, catalog, owner)
        assert report == ScheduleSyncReport(created=(b.id,), unchanged=(a.id,))
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_the_default_servable_check_follows_the_registered_workflows(
    temporal_client: Client,
    owner: str,
    catalog: tuple[ScheduleEntry, ScheduleEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker.temporal import queues

    a, b = catalog
    ids = [a.id, b.id]
    monkeypatch.setattr(queues, "served_workflow_types", lambda: frozenset({a.workflow.value}))
    try:
        report = await sync_schedules(temporal_client, catalog, managed_by=owner, dry_run=True)
        assert report == ScheduleSyncReport(created=(a.id,), pending=(b.id,))
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_dry_run_reports_every_change_and_writes_nothing(
    temporal_client: Client, owner: str, catalog: tuple[ScheduleEntry, ScheduleEntry]
) -> None:
    a, b = catalog
    ids = [a.id, b.id]
    try:
        report = await _sync(temporal_client, catalog, owner, dry_run=True)
        assert report == ScheduleSyncReport(created=(a.id, b.id))
        assert await _describe_or_none(temporal_client, a.id) is None
        await _sync(temporal_client, catalog, owner)
        await _wait_listed(temporal_client, owner, set(ids))
        moved = ScheduleEntry(a.id, a.workflow, cron="45 2 * * *", catchup_window=a.catchup_window)
        report = await _sync(temporal_client, (moved,), owner, dry_run=True)
        assert report == ScheduleSyncReport(updated=(a.id,), deleted=(b.id,))
        described = await temporal_client.get_schedule_handle(a.id).describe()
        assert fingerprint(described.schedule) == fingerprint(a.to_schedule()), "not updated"
        assert await _describe_or_none(temporal_client, b.id) is not None, "not deleted"
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
async def test_a_concurrent_creator_is_converged_not_failed(
    temporal_client: Client,
    owner: str,
    catalog: tuple[ScheduleEntry, ScheduleEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two default-queue workers booting together: the second sees no managed
    schedules yet (the list lags), tries to create, hits 'already running', and
    converges on what exists instead of crashing the boot."""
    ids = [e.id for e in catalog]
    try:
        await _sync(temporal_client, catalog, owner)

        async def lagging_list(client: Any, managed_by: str) -> set[str]:
            return set()

        monkeypatch.setattr(schedules_mod, "_managed_ids", lagging_list)
        report = await _sync(temporal_client, catalog, owner)
        assert report == ScheduleSyncReport(unchanged=tuple(ids))
    finally:
        await _cleanup(temporal_client, ids)


@pytest.mark.temporal
@pytest.mark.parametrize("entry", SCHEDULES, ids=lambda e: e.id)
async def test_every_catalog_entry_round_trips_the_server_unchanged(
    temporal_client: Client, entry: ScheduleEntry
) -> None:
    """The lockstep proof behind 'a second sync changes nothing': the server's
    normalised form of each real entry fingerprints equal to the entry itself,
    so the catalog never oscillates between created and updated."""
    schedule_id = f"roundtrip-{entry.id}-{uuid4().hex[:8]}"
    desired = entry.to_schedule()
    desired.action.id = schedule_id  # type: ignore[union-attr]
    try:
        handle = await temporal_client.create_schedule(schedule_id, desired)
        described = await handle.describe()
        assert fingerprint(described.schedule) == fingerprint(desired)
        assert described.schedule.spec.cron_expressions == [], "the server keeps calendars"
    finally:
        await _cleanup(temporal_client, [schedule_id])


@workflow.defn(name="stub.sleeper", sandboxed=False)
class Sleeper:
    @workflow.run
    async def run(self) -> None:
        await workflow.sleep(2.5)


@pytest.mark.temporal
async def test_overlap_skip_drops_a_tick_while_the_previous_run_is_going(
    temporal_worker: Any, temporal_client: Client, owner: str
) -> None:
    """The one wall-clock case: a 1 s interval driving a 2.5 s workflow under
    the catalog's policy must skip ticks rather than stack runs."""
    schedule_id = f"{owner}-overlap"
    async with temporal_worker(workflows=[Sleeper]) as running:
        try:
            await temporal_client.create_schedule(
                schedule_id,
                Schedule(
                    action=ScheduleActionStartWorkflow(
                        Sleeper.run, id=schedule_id, task_queue=running.task_queue
                    ),
                    spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=1))]),
                    policy=SchedulePolicy(
                        overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=1)
                    ),
                ),
            )
            handle = temporal_client.get_schedule_handle(schedule_id)
            skipped = 0
            for _ in range(80):
                await asyncio.sleep(0.1)
                info = (await handle.describe()).info
                assert len(info.running_actions) <= 1
                skipped = info.num_actions_skipped_overlap
                if skipped >= 1 and info.num_actions >= 1:
                    break
            assert skipped >= 1
        finally:
            await _cleanup(temporal_client, [schedule_id])


def test_production_marker_is_the_documented_one() -> None:
    assert MANAGED_BY == "alkera-worker"
    assert MANAGED_MEMO_KEY == "managed_by"


# --- The Files family follows FILES_ENABLED ----------------------------------


@pytest.mark.parametrize(
    "workflow_type",
    [pytest.param(w, id=w.value) for w in sorted(schedules_mod._FILES_WORKFLOWS, key=str)],
)
def test_a_files_workflow_is_not_servable_without_the_files_store(
    workflow_type: WorkflowType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry is a module walk, so every Files workflow is "registered" even
    on a deployment with FILES_ENABLED=false — where nothing ever installs the
    store factory and the activity raises on every run. Left alone, the daily
    `files.gc` schedule would be created there and burn its whole transient retry
    budget every night, on the one signal (the failed-workflow count) an operator
    would otherwise trust."""
    from alkera_core.config import settings as core_settings
    from worker.temporal import queues

    monkeypatch.setattr(queues, "served_workflow_types", lambda: frozenset({workflow_type.value}))
    # The collection pass has its own switch on top; it is on here so this test
    # is about the Files store alone, whatever the developer's .env says.
    monkeypatch.setattr(core_settings, "files_gc_enabled", True)
    monkeypatch.setattr(core_settings, "files_enabled", True)
    assert schedules_mod._default_servable(workflow_type) is True
    monkeypatch.setattr(core_settings, "files_enabled", False)
    assert schedules_mod._default_servable(workflow_type) is False


def test_a_non_files_workflow_is_unaffected_by_files_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is scoped to the family that needs the store — turning Files off
    must not silently stop any other job."""
    from alkera_core.config import settings as core_settings
    from worker.temporal import queues

    other = WorkflowType.PRUNE_EXPIRED_TOKENS
    assert other not in schedules_mod._FILES_WORKFLOWS
    monkeypatch.setattr(queues, "served_workflow_types", lambda: frozenset({other.value}))
    monkeypatch.setattr(core_settings, "files_enabled", False)
    assert schedules_mod._default_servable(other) is True


def test_every_scheduled_files_entry_is_covered_by_the_guard() -> None:
    """A future Files schedule added to the catalog must be listed in the guard,
    or it reintroduces the nightly failure on every non-Files deployment."""
    scheduled_files = {
        entry.workflow for entry in SCHEDULES if entry.workflow.value.startswith("files.")
    }
    assert scheduled_files
    assert scheduled_files <= schedules_mod._FILES_WORKFLOWS


def test_a_registered_set_joins_the_platforms_schedules_in_order() -> None:
    own = (ScheduleEntry("a", WorkflowType.FILES_GC, cron="0 4 * * *"),)
    theirs = (ScheduleEntry("b", WorkflowType.FILES_JANITOR, cron="0 5 * * *"),)
    assert [e.id for e in merge_schedules(own, (theirs,))] == ["a", "b"]


def test_with_nothing_registered_the_catalog_is_the_platforms() -> None:
    own = (ScheduleEntry("a", WorkflowType.FILES_GC, cron="0 4 * * *"),)
    assert merge_schedules(own, ()) == own


def test_a_schedule_id_declared_twice_is_refused() -> None:
    own = (ScheduleEntry("a", WorkflowType.FILES_GC, cron="0 4 * * *"),)
    theirs = (ScheduleEntry("a", WorkflowType.FILES_JANITOR, cron="0 5 * * *"),)
    with pytest.raises(ValueError, match="declared twice"):
        merge_schedules(own, (theirs,))
