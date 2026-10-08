"""The daemon scheduler's persisted model + store.

This module owns the on-disk job state and the **atomic claim** that makes the
scheduler safe when two daemons run against the same workspace (e.g. two VS Code
windows). Both the per-second
beat and an IDE ``run_now`` go through ``SchedulerStore.claim``; a compare-and-set
under the store's own ``FileLock`` guarantees exactly one runner per job, and a
crashed runner's **stale lease** is reclaimed by the next claimer.

Layering: the store lives here (alkera_cli) because it owns the job model + lease
logic; it persists under ``.alkera/scheduler/`` (``ProjectDirectory.scheduler_path``)
using api-core's ``FileLock`` / ``write_json_atomic`` primitives, never the other
way around: api-core owns locations and primitives, not CLI models.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alkera_core.atomic_io import write_json_atomic
from alkera_core.process import process_alive
from alkera_core.project.locking import FileLock, retrying_lock
from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from alkera_cli.plugins.plugin_base.progress import read_progress

logger = logging.getLogger(__name__)

#: A running job whose lease heartbeat is older than this is presumed crashed and
#: may be reclaimed by another daemon (belt-and-braces alongside PID liveness).
DEFAULT_LEASE_TTL_SECONDS = 60.0

JobState = Literal["scheduled", "running", "ok", "failed"]

#: Transient payload marker (set by ``run_now``, NEVER persisted) telling a runner this run
#: is an explicit user "Run now" and must FORCE through its skip-unchanged gate — a manual
#: trigger should do visible work even when the source is unchanged. Runners that don't gate
#: simply ignore it. It rides on an in-memory job copy so the on-disk payload stays clean.
FORCE_RUN_KEY = "__force_run__"


# ---------------------------------------------------------------------------
# Triggers — a discriminated union with a Raw fallback (forward-compat)
# ---------------------------------------------------------------------------


class _TriggerBase(BaseModel):
    # extra="allow" so a newer writer's extra trigger field survives an older
    # reader's round-trip (mirrors VersionedModel's forward-compat stance).
    model_config = ConfigDict(extra="allow")

    def next_after(self, after: datetime) -> datetime | None:
        """The next fire time strictly after ``after`` (None = never / not
        time-driven)."""
        return None

    def is_standing(self) -> bool:
        """A STANDING trigger fires repeatedly on an external signal (``run_now``) and is
        idle — re-runnable — between fires, with no scheduled next time. Distinguishes an
        event-driven manual job (re-runnable forever) from a one-shot (``once``, terminal
        after it fires). Only :class:`EventTrigger` is standing; everything else is either
        time-driven (a ``next_after``) or a genuine one-shot."""
        return False


class CronTrigger(_TriggerBase):
    type: Literal["cron"] = "cron"
    expr: str
    timezone: str = "UTC"

    def next_after(self, after: datetime) -> datetime | None:
        # Compute the next fire in the configured timezone, then store as UTC so
        # the scheduler's now-comparison stays consistent. A bad tz name degrades
        # to UTC rather than crashing the beat.
        tz: tzinfo
        try:
            tz = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            tz = UTC
        aware = after if after.tzinfo else after.replace(tzinfo=UTC)
        base = aware.astimezone(tz)
        nxt = croniter(self.expr, base).get_next(datetime)
        nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=tz)
        return nxt.astimezone(UTC)


class IntervalTrigger(_TriggerBase):
    type: Literal["interval"] = "interval"
    seconds: float

    def next_after(self, after: datetime) -> datetime | None:
        base = after if after.tzinfo else after.replace(tzinfo=UTC)
        return base + timedelta(seconds=self.seconds)


class OnceTrigger(_TriggerBase):
    type: Literal["once"] = "once"
    run_at: datetime

    def next_after(self, after: datetime) -> datetime | None:
        run_at = self.run_at if self.run_at.tzinfo else self.run_at.replace(tzinfo=UTC)
        base = after if after.tzinfo else after.replace(tzinfo=UTC)
        # ``>=`` so a one-shot scheduled for *now* (run_at == registration time) is
        # immediately due — with strict ``>`` it primed next_run_at=None and the
        # beat never fired it (kb_seed-on-open was a silent no-op). After the run,
        # complete() re-checks next_after(completion_time); completion is strictly
        # later than run_at on a real clock, so it correctly goes terminal (None).
        return run_at if run_at >= base else None


class EventTrigger(_TriggerBase):
    type: Literal["event"] = "event"
    event: str
    # Event-driven — never time-scheduled; fired via run_now on the event.

    def is_standing(self) -> bool:
        # Re-runnable forever: a metered/manual refresh fires on demand, then returns to
        # idle waiting for the next fire — it is NOT a one-shot (see complete()).
        return True


class RawTrigger(_TriggerBase):
    """Forward-compat fallback for a trigger ``type`` a newer writer introduced."""

    type: str = "__unknown__"


_trigger_tag = make_unknown_tag_discriminator({"cron", "interval", "once", "event"}, field="type")

Trigger = Annotated[
    Annotated[CronTrigger, Tag("cron")]
    | Annotated[IntervalTrigger, Tag("interval")]
    | Annotated[OnceTrigger, Tag("once")]
    | Annotated[EventTrigger, Tag("event")]
    | Annotated[RawTrigger, Tag("__unknown__")],
    Discriminator(_trigger_tag),
]


# ---------------------------------------------------------------------------
# Lease + the persisted job
# ---------------------------------------------------------------------------


def _hostname() -> str:
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    # Cross-platform: on Windows `os.kill(pid, 0)` routes to TerminateProcess
    # and would KILL the lease holder, so liveness goes through the shared util.
    return process_alive(pid)


#: Characters Windows forbids in filenames (plus ``%``, our escape char).
#: Job ids are ``<plugin>:<connection>`` by convention — the ``:`` alone makes
#: the raw id unusable as an NTFS filename (it's the alternate-data-stream
#: separator; `os.replace` fails with WinError 87).
_FILENAME_UNSAFE = set('<>:"/\\|?*%')


def _job_filename(job_id: str) -> str:
    """Deterministic, cross-platform-safe filename for a job id.

    Percent-encodes the characters Windows can't store in a filename (and
    ``%`` itself, so the mapping stays injective). Identical output on every
    platform so a workspace moved between OSes keeps addressing the same
    files. ``list_jobs`` scans by glob + content, so it never depends on
    this mapping.
    """
    return "".join(
        f"%{ord(ch):02X}" if ch in _FILENAME_UNSAFE or ord(ch) < 32 else ch for ch in job_id
    )


def progress_sidecar_path(scheduler_root: Path, job_id: str) -> Path:
    """Where a seed worker writes its live progress for ``job_id`` — under a ``progress/``
    sibling of the jobs dir (NOT inside it, so ``list_jobs`` 's ``*.json`` glob never sees
    it). The daemon merges it into ``scheduler.list`` and clears it on completion."""
    return scheduler_root / "progress" / f"{_job_filename(job_id)}.json"


def _run_outcome(
    ok: bool, error: str | None, summary: dict[str, Any] | None
) -> tuple[str, str | None]:
    """Classify how a run ended, from the runner's ok/error + the worker's progress
    sidecar. ``complete`` = did everything; ``incomplete`` = budgeted/cut-off, more next
    run; ``failed`` = errored. The worker writes a terminal record (``done``/``complete``/
    ``message``) when it finishes cleanly; a sidecar WITHOUT ``done`` means it was
    interrupted (timeout / kill) mid-run; no sidecar means there was nothing to do."""
    if error == "cancelled":
        # A deliberate stop (user cancel / shutdown) — NOT a failure to investigate.
        return "cancelled", "Cancelled before finishing"
    if not ok:
        return "failed", error
    if summary and summary.get("done"):
        outcome = "complete" if summary.get("complete", True) else "incomplete"
        return outcome, summary.get("message")
    if summary is not None:  # progress but no terminal record → cut off before finishing
        cur, tot = summary.get("current"), summary.get("total")
        tail = f" ({cur}/{tot} done)" if isinstance(cur, int) and isinstance(tot, int) else ""
        return "incomplete", f"Interrupted before finishing — resumes next run{tail}"
    return "complete", None  # no sidecar → nothing to do this run (e.g. unchanged)


class JobLease(BaseModel):
    """Which daemon currently owns a running job. A live lease blocks other
    daemons; a stale one (dead PID on the same host, or — cross-host only — a
    heartbeat past the TTL) is reclaimable."""

    model_config = ConfigDict(extra="allow")

    pid: int
    host: str
    claimed_at: datetime
    heartbeat_at: datetime

    def is_stale(self, *, now: datetime, ttl_seconds: float) -> bool:
        if self.host == _hostname():
            # Same host: PID liveness is authoritative. A dead holder is stale
            # immediately; a LIVE one is never stale by heartbeat age alone —
            # after a laptop suspend every local daemon resumes with an
            # hours-old heartbeat, and an age-based reclaim here would have a
            # second daemon double-run the job the live holder is still
            # running. (A hung-but-alive holder keeps its lease until it
            # exits; complete()'s ownership guard prevents state clobbering
            # either way.)
            return not _pid_alive(self.pid)
        # Cross-host there is no PID to check — heartbeat age is all we have.
        age = (now - self.heartbeat_at).total_seconds()
        return age > ttl_seconds


class ScheduledJob(VersionedModel):
    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    job_id: str
    plugin: str = ""
    kind: str = ""
    trigger: Trigger
    payload: dict[str, Any] = Field(default_factory=dict)
    state: JobState = "scheduled"
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None
    lease: JobLease | None = None
    priority: int = 0
    """Higher runs first when several jobs are due in the same beat (from
    ``RefreshSpec.priority``). Ties break on ``job_id`` for a deterministic order."""
    coalesce_key: str | None = None
    """Jobs sharing a non-None key never run concurrently — while one is in flight the
    beat defers its due siblings (from ``RefreshSpec.coalesce_key``). ``None`` = no
    coalescing. Old job files (pre-1.2.0) default to ``None`` → unchanged behavior."""
    last_outcome: str | None = None
    """How the most recent run ended: ``complete`` (did everything it set out to),
    ``incomplete`` (made progress but has more to do on the next run — a budgeted /
    interrupted seed), or ``failed`` (errored). Open taxonomy (plain str) so a newer
    writer's value survives an older reader. ``None`` = never run."""
    last_message: str | None = None
    """A short human summary of the most recent run (e.g. "Column lineage incomplete —
    more to parse next run"), surfaced in the Background Jobs UI."""

    def is_due(self, *, now: datetime, ttl_seconds: float) -> bool:
        """A job the beat should try to claim now: time has arrived AND it's
        either idle (``scheduled``) or a ``running`` job whose runner died."""
        if self.next_run_at is None or self.next_run_at > now:
            return False
        if self.state == "scheduled":
            return True
        if self.state == "running" and self.lease is not None:
            return self.lease.is_stale(now=now, ttl_seconds=ttl_seconds)
        return False


# ---------------------------------------------------------------------------
# The store — atomic claim under a per-store FileLock
# ---------------------------------------------------------------------------


class SchedulerStore:
    """Persists jobs under ``.alkera/scheduler/jobs/<job_id>.json`` and serializes
    every mutation under ``.alkera/scheduler/.lock`` (its own lock, never
    inherited). The brief per-mutation lock + the long per-run job lease
    together give two-daemon safety + crash recovery."""

    def __init__(
        self,
        root: Path,
        *,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self._root = Path(root)
        self._jobs = self._root / "jobs"
        self._jobs.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self._root / ".lock")
        self._ttl = lease_ttl_seconds
        self._lock_timeout = lock_timeout_seconds

    def _locked(self) -> AbstractContextManager[None]:
        """Acquire the store lock, RETRYING while another daemon holds it.

        Unlike the chat lock (one client owns a chat for its lifetime, so
        ``LockHeldError`` is the right fail-fast), this lock is held only for the
        microseconds of a single read-modify-write and is contended by every
        daemon's beat/run_now. So we spin briefly until it's free (a held lock is
        not an error here) — a stale holder is still reclaimed by FileLock itself.
        The spin-on-contention primitive is shared with the cost ledger.
        """
        return retrying_lock(self._lock, timeout_seconds=self._lock_timeout)

    # --- paths / io ----------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self._jobs / f"{_job_filename(job_id)}.json"

    def progress(self, job_id: str) -> dict[str, Any] | None:
        """The live progress a running seed worker reported for ``job_id`` (status text +
        optional current/total), or None when there's no sidecar (idle / no detail)."""
        return read_progress(progress_sidecar_path(self._root, job_id))

    def clear_progress(self, job_id: str) -> None:
        """Drop a job's progress sidecar (best-effort) — called when it completes or is
        removed, so a finished job never shows a stale bar."""
        with contextlib.suppress(FileNotFoundError, OSError):
            progress_sidecar_path(self._root, job_id).unlink()

    def _read(self, job_id: str) -> ScheduledJob | None:
        path = self._path(job_id)
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return None
        return ScheduledJob.model_validate_json(raw)

    def _write(self, job: ScheduledJob) -> None:
        write_json_atomic(self._path(job.job_id), job.model_dump(mode="json"))

    # --- reads ---------------------------------------------------------

    def get(self, job_id: str) -> ScheduledJob | None:
        return self._read(job_id)

    def list_jobs(self) -> list[ScheduledJob]:
        out: list[ScheduledJob] = []
        for path in sorted(self._jobs.glob("*.json")):
            try:
                out.append(ScheduledJob.model_validate_json(path.read_text()))
            except (OSError, ValueError):
                continue  # skip a torn/corrupt file rather than crash the beat
        return out

    def due(self, *, now: datetime) -> list[ScheduledJob]:
        return [j for j in self.list_jobs() if j.is_due(now=now, ttl_seconds=self._ttl)]

    def active_coalesce_keys(self, *, now: datetime) -> set[str]:
        """The ``coalesce_key``s of jobs currently RUNNING with a live (non-stale) lease.
        The beat defers a due job whose key is in this set, so two jobs that must not run
        at once never both start — a guarantee that holds across daemons (it reads the
        persisted running set), not just within one process's in-memory seed lock. A
        stale-lease (dead-runner) job is excluded so a crashed worker can't block its
        siblings forever — its key frees up the moment its lease ages out."""
        keys: set[str] = set()
        for job in self.list_jobs():
            if (
                job.coalesce_key
                and job.state == "running"
                and job.lease is not None
                and not job.lease.is_stale(now=now, ttl_seconds=self._ttl)
            ):
                keys.add(job.coalesce_key)
        return keys

    # --- mutations (all under the store lock) --------------------------

    def register(self, job: ScheduledJob) -> ScheduledJob:
        """Insert a job if absent; return the stored job (existing wins — register
        is idempotent so a restart doesn't duplicate a plugin's standing jobs)."""
        with self._locked():
            existing = self._read(job.job_id)
            if existing is not None:
                return existing
            self._write(job)
            return job

    def remove(self, job_id: str) -> bool:
        with self._locked():
            path = self._path(job_id)
            if not path.exists():
                return False
            path.unlink()
            self.clear_progress(job_id)
            return True

    def prime_due(self, job_id: str, *, now: datetime) -> ScheduledJob | None:
        """Make a NEVER-RUN, scheduled job due NOW — so a refresh fires on activation /
        daemon restart instead of waiting a full cadence. A no-op once the job has run
        (``last_run_at`` set → respect its cadence), if it isn't ``scheduled`` (a running
        job is left to its runner), or if it's already due. Returns the job if primed."""
        with self._locked():
            job = self._read(job_id)
            if job is None or job.state != "scheduled" or job.last_run_at is not None:
                return None
            # A STANDING job (EventTrigger) is a MANUAL-ONLY source — a metered/billed refresh
            # (Snowflake ACCESS_HISTORY, BigQuery JOBS, Databricks system.access). It must NEVER
            # be primed to auto-fire, or activation / a daemon restart would trigger a billed
            # query. Priming is for auto-cadence jobs only; a standing job stays idle until an
            # explicit run. (Defense in depth — callers already skip metered jobs.)
            if job.trigger.is_standing():
                return None
            if job.next_run_at is not None and job.next_run_at <= now:
                return job  # already due — nothing to do
            job.next_run_at = now
            self._write(job)
            return job

    def reschedule_soon(self, job_id: str, *, now: datetime) -> ScheduledJob | None:
        """Make an ALREADY-RUN recurring job due NOW so the next beat re-claims it — the file
        watcher's nudge when a connection's source changed. Unlike ``prime_due`` (never-run
        only) it works after the job has run, and unlike ``run_now`` it does NOT force: the
        beat-claimed run goes through the normal skip-gate, so an unchanged source is still a
        cheap no-op. Returns the job if it was made due (or already was); ``None`` if missing,
        not time-triggered (one-shot/event), or currently RUNNING.

        A nudge that lands while the job is RUNNING is NOT dropped: it sets a one-shot
        ``rerun_requested`` marker that :meth:`complete` honors by re-running immediately instead
        of waiting for the cadence — so work that arrived mid-run (e.g. a table grain finishing
        while the column drain is in flight) is never stranded for a whole cycle."""
        with self._locked():
            job = self._read(job_id)
            if job is None:
                return None
            if job.trigger.next_after(now) is None:
                return None  # one-shot already fired / event-driven — nothing to re-schedule
            if job.state == "running":
                # Don't lose the signal: mark the in-flight run to re-run on completion.
                # Idempotent — a burst of nudges sets one marker, one re-run.
                if not job.metadata.get("rerun_requested"):
                    job.metadata["rerun_requested"] = True
                    self._write(job)
                return None
            if job.state != "scheduled":
                return None
            if job.next_run_at is not None and job.next_run_at <= now:
                return job  # already due — nothing to do
            job.next_run_at = now
            self._write(job)
            return job

    def claim(
        self, job_id: str, *, now: datetime, respect_coalesce: bool = False
    ) -> ScheduledJob | None:
        """Atomically transition ``scheduled`` (or stale-``running``) → ``running``
        and stamp our lease. Returns the claimed job, or ``None`` if another live
        runner already holds it. This is the two-daemon-safety chokepoint: the
        store lock serializes the read-modify-write so exactly one daemon wins.

        With ``respect_coalesce`` (the beat sets it), the claim ALSO refuses if a live
        sibling — another running job sharing this job's ``coalesce_key`` — already holds
        the key. Because that check and the claim happen under the SAME store lock, it
        closes the cross-daemon TOCTOU that the beat's in-memory pre-filter alone cannot:
        two daemons can't each read "no sibling running" and then both claim DIFFERENT
        siblings of one key. ``run_now`` leaves it off — an explicit user action runs even
        past a sibling (the runtime's in-process seed lock still serializes the heavy work)."""
        with self._locked():
            job = self._read(job_id)
            if job is None:
                return None
            if job.state == "running" and job.lease is not None:
                if not job.lease.is_stale(now=now, ttl_seconds=self._ttl):
                    return None  # a live runner owns it
            if respect_coalesce and job.coalesce_key is not None:
                for other in self.list_jobs():
                    if (
                        other.job_id != job_id
                        and other.coalesce_key == job.coalesce_key
                        and other.state == "running"
                        and other.lease is not None
                        and not other.lease.is_stale(now=now, ttl_seconds=self._ttl)
                    ):
                        return None  # a coalesce sibling is in flight → defer this beat
            if job.state in ("scheduled", "running"):
                job.state = "running"
                job.lease = JobLease(
                    pid=os.getpid(), host=_hostname(), claimed_at=now, heartbeat_at=now
                )
                # A NEW run is starting → drop the previous run's outcome/error so the UI never
                # shows a stale "cancelled"/"failed" badge (or red error) on a job that is RUNNING
                # again (the "it says cancelled on the running job" glitch). ``last_run_at`` is
                # left so "ran Xm ago" still reads true; ``complete()`` sets the fresh result.
                job.last_outcome = None
                job.last_message = None
                job.last_error = None
                self._write(job)
                return job
            return None

    def reset_orphaned_running(self, *, now: datetime) -> list[str]:
        """Crash recovery: a job left ``running`` by a daemon that DIED — its lease is stale
        (dead PID same-host, or past the TTL cross-host) — is reset to ``scheduled``. A
        time-driven job becomes due NOW so the interrupted run resumes; a STANDING (event /
        metered) job is reset to IDLE (``next_run_at=None``) instead — re-arming it due-now
        would make the beat AUTO-RUN a manual-only metered source (a billed warehouse query)
        on daemon restart, which must never happen without an explicit user trigger. Mirrors
        ``complete()``'s standing-job branch.

        Otherwise it lingers as a PHANTOM 'running' row (e.g. after you close + reopen the
        IDE): no one is running it, but ``state == 'running'`` shows it as Running in the Jobs
        UI and — when it's coalesce-deferred behind a live sibling — the beat never re-claims
        it, so it never self-heals. After the reset a time-driven job re-claims cleanly
        (coalesced → it shows as Queued); a standing job stays idle until the user runs it.
        SAFE across daemons: a job a LIVE daemon owns has a non-stale lease and is left alone.
        Returns the job ids reset."""
        reset: list[str] = []
        with self._locked():
            for job in self.list_jobs():
                if (
                    job.state == "running"
                    and job.lease is not None
                    and job.lease.is_stale(now=now, ttl_seconds=self._ttl)
                ):
                    job.state = "scheduled"
                    job.lease = None
                    # interrupted → due now (re-run; coalesce defers it), EXCEPT a standing
                    # (metered/manual) job stays idle so it never auto-runs a billed source.
                    job.next_run_at = None if job.trigger.is_standing() else now
                    self._write(job)
                    self.clear_progress(job.job_id)  # drop the dead daemon's frozen progress
                    reset.append(job.job_id)
        return reset

    def heartbeat(self, job_id: str, *, now: datetime) -> bool:
        """Refresh our lease so the job isn't reclaimed mid-run. No-op (False) if
        we no longer hold it (another daemon reclaimed a presumed-dead lease)."""
        with self._locked():
            job = self._read(job_id)
            if job is None or job.lease is None:
                return False
            if job.lease.pid != os.getpid() or job.lease.host != _hostname():
                return False
            job.lease = job.lease.model_copy(update={"heartbeat_at": now})
            self._write(job)
            return True

    def complete(
        self,
        job_id: str,
        *,
        now: datetime,
        ok: bool,
        error: str | None = None,
    ) -> ScheduledJob | None:
        """Finish a run: set ``ok``/``failed``, clear the lease, advance
        ``next_run_at`` from the trigger (recurring jobs go back to
        ``scheduled``; one-shots with no next time stay terminal)."""
        with self._locked():
            job = self._read(job_id)
            if job is None:
                return None
            # Ownership guard: only the daemon that currently HOLDS the lease
            # may finalize. If we were reclaimed (the lease now belongs to another
            # pid/host) or the job isn't running, do NOT clobber the new owner —
            # else a slow runner finishing after a stale-lease reclaim would reset
            # the job and let it double-run (the exact invariant claim() protects).
            if job.lease is None or job.lease.pid != os.getpid() or job.lease.host != _hostname():
                return None
            # Record HOW the run ended (read the worker's terminal sidecar BEFORE clearing
            # it) so the UI can tell "did everything" from "more to do next run".
            job.last_outcome, job.last_message = _run_outcome(ok, error, self.progress(job_id))
            job.last_run_at = now
            # A deliberate Cancel is NOT a failure — don't surface it in the red error alert
            # (the "cancelled" outcome/message already convey it). Only a real error is recorded.
            job.last_error = None if error == "cancelled" else error
            job.lease = None
            nxt = job.trigger.next_after(now)
            if nxt is not None:
                job.state = "scheduled"
                # A nudge that arrived while we were running (rerun_requested) → become due NOW
                # so the mid-run work is picked up promptly, not at the next cadence. EXCEPT a
                # deliberate Cancel must STICK: re-arming a just-cancelled run due-now reads as
                # "I cancelled but it came right back" — so a cancel drops the marker and falls
                # back to the normal cadence. Pop the one-shot marker either way.
                rerun = job.metadata.pop("rerun_requested", None)
                job.next_run_at = now if (rerun and error != "cancelled") else nxt
            elif job.trigger.is_standing():
                # An event-driven STANDING job (a manual/metered refresh) is re-runnable: it
                # returns to idle ``scheduled`` with NO ``next_run_at`` (so ``is_due`` never makes
                # it auto-run — it needs a time), and a later ``run_now`` can claim it again.
                # Without this it would go terminal (``ok``/``failed``) after a single run and
                # ``claim`` would refuse it forever — a metered job runnable exactly once.
                job.state = "scheduled"
                job.next_run_at = None
                job.metadata.pop("rerun_requested", None)
            else:
                job.state = "ok" if ok else "failed"
                job.next_run_at = None
            self._write(job)
            self.clear_progress(job_id)  # the run is done — drop its live progress bar
            return job


# ---------------------------------------------------------------------------
# The engine — beat + run_now, dispatching to per-kind runners off the loop
# ---------------------------------------------------------------------------

JobRunner = Callable[["ScheduledJob"], Awaitable[None]]


@dataclass(frozen=True)
class JobEvent:
    """Emitted as a ``scheduler.job_event`` notification by the daemon."""

    job_id: str
    kind: str
    phase: Literal["started", "completed", "failed"]
    reason: str = ""
    error: str | None = None


JobEventListener = Callable[[JobEvent], None]


class Scheduler:
    """Per-project orchestrator over a :class:`SchedulerStore`.

    The per-second beat (``tick``) and an IDE ``run_now`` both go through the
    store's atomic claim, then dispatch the job to its registered ``kind`` runner
    as a background task — OFF the JSON-RPC request loop. A long run is
    kept alive by a heartbeat task so another daemon won't reclaim it mid-flight.
    """

    def __init__(
        self,
        store: SchedulerStore,
        *,
        clock: Callable[[], datetime] | None = None,
        heartbeat_interval_seconds: float = 20.0,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._heartbeat_interval = heartbeat_interval_seconds
        self._runners: dict[str, JobRunner] = {}
        self._listeners: list[JobEventListener] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # --- wiring --------------------------------------------------------

    def register_runner(self, kind: str, runner: JobRunner) -> None:
        """Bind a ``kind`` to the coroutine that executes it (e.g. Phase-4
        RefreshProviders register ``kb_reenrich`` / ``lineage_poll``)."""
        self._runners[kind] = runner

    def registered_kinds(self) -> set[str]:
        """The job kinds with a runner bound right now — lets a caller (the daemon beat)
        cheaply self-heal a registration gap without re-arming jobs."""
        return set(self._runners)

    def on_event(self, listener: JobEventListener) -> None:
        """Subscribe to job lifecycle events (the daemon forwards them as
        ``scheduler.job_event`` notifications)."""
        self._listeners.append(listener)

    def register(self, job: ScheduledJob) -> ScheduledJob:
        return self._store.register(job)

    def prime_due(self, job_id: str, *, now: datetime | None = None) -> ScheduledJob | None:
        """Make a never-run scheduled job due now (fire on activation/restart). See
        :meth:`SchedulerStore.prime_due`."""
        return self._store.prime_due(job_id, now=now or self._clock())

    def reschedule_soon(self, job_id: str, *, now: datetime | None = None) -> ScheduledJob | None:
        """Nudge an already-run recurring job to run on the next beat through the GATED path
        (not forced) — the file watcher's trigger. See :meth:`SchedulerStore.reschedule_soon`."""
        return self._store.reschedule_soon(job_id, now=now or self._clock())

    def prune_orphans(self, *, protected: set[str]) -> list[str]:
        """Remove persisted jobs whose ``kind`` has NO registered runner — orphans from a
        renamed/removed kind (e.g. the old ``lineage_refresh``) that would otherwise sit
        in the Jobs UI forever (the beat already declines to run them). ``protected`` kinds
        are NEVER pruned even without a runner here: their runners register on a different
        path (the KB jobs) or in another process, so absence isn't proof of orphanhood.
        Call only once a project's own runners are registered. Returns the ids removed."""
        removed: list[str] = []
        for job in self._store.list_jobs():
            if job.kind in self._runners or job.kind in protected:
                continue
            if self._store.remove(job.job_id):
                removed.append(job.job_id)
        if removed:
            logger.info("scheduler pruned %d orphan job(s): %s", len(removed), removed)
        return removed

    def list_jobs(self) -> list[ScheduledJob]:
        return self._store.list_jobs()

    def job_progress(self, job_id: str) -> dict[str, Any] | None:
        """The live progress a running worker reported for ``job_id`` (or None)."""
        return self._store.progress(job_id)

    def stop(self, job_id: str) -> bool:
        """Stop a job's IN-FLIGHT run WITHOUT deleting the job — the user-facing "Cancel".
        Cancels the running task (its cleanup kills the seed subprocess; the task's
        ``complete()`` records the cancelled outcome and RESCHEDULES the recurring job for
        its next cadence), so the job stays and runs again. A no-op (returns False) if the
        job isn't currently running. Jobs are PERMANENT from the UI; there is deliberately
        no user affordance to delete one (only the internal ``remove`` below)."""
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def remove(self, job_id: str) -> bool:
        """Delete a job entirely (cancel any run + drop its persisted state). INTERNAL only
        — a connection was removed / a plugin disabled / a stale job is being replaced. NOT
        wired to the Background Jobs UI, where jobs are permanent and "Cancel" maps to
        :meth:`stop`."""
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return self._store.remove(job_id)

    # --- the beat + run_now -------------------------------------------

    async def tick(self) -> list[str]:
        """One beat: claim every due job and spawn its run. Returns the job_ids
        started this tick (claimed by THIS scheduler).

        A due job whose ``kind`` has no registered runner is SKIPPED, never claimed —
        either its plugin's runners aren't registered in this process yet (it'll run once
        they are) or it's an orphan from a renamed/removed kind. Dispatching it would
        ``complete()`` it as failed and (for a recurring trigger) requeue it, so the beat
        would re-fail it forever — the ``lineage_refresh`` crash-loop. An explicit
        ``run_now`` still surfaces the "no runner" error (that's a user-triggered action)."""
        now = self._clock()
        started: list[str] = []
        # Crash recovery FIRST: a job left "running" by a dead daemon (stale lease — e.g. you
        # closed + reopened the IDE) is reset to scheduled, so it can't linger as a phantom
        # "running" row (which the coalesce check below would otherwise keep deferred forever).
        self._store.reset_orphaned_running(now=now)
        # Higher priority first; ties on job_id so the order is deterministic across beats.
        due = sorted(self._store.due(now=now), key=lambda j: (-j.priority, j.job_id))
        # Coalesce: a key already in flight (running, live lease — possibly under another
        # daemon) blocks its due siblings THIS beat. They stay scheduled+due and are
        # retried next beat, so the family runs strictly one-at-a-time without a shared
        # in-process lock. `active` is a CHEAP pre-filter (skip a lock acquisition for an
        # obviously-deferred job) + it accumulates keys we claim below so two due siblings
        # in the SAME beat can't both start; the AUTHORITATIVE guard is inside `claim`
        # (`respect_coalesce`), which re-checks under the store lock so two daemons racing
        # can't both claim different siblings of one key (the pre-filter alone has a TOCTOU).
        active = self._store.active_coalesce_keys(now=now)
        for job in due:
            if job.kind not in self._runners:
                continue  # no runner here → don't claim+crash-loop; retried when one registers
            if job.coalesce_key is not None and job.coalesce_key in active:
                continue  # a sibling is already in flight → defer (stays scheduled)
            claimed = self._store.claim(job.job_id, now=now, respect_coalesce=True)
            if claimed is not None and self._spawn(claimed, reason="cron"):
                started.append(claimed.job_id)
                if job.coalesce_key is not None:
                    active.add(job.coalesce_key)
        return started

    async def run_now(self, job_id: str, *, reason: str = "run_now") -> bool:
        """Claim + start a job immediately (same atomic path as the beat, so it can't
        double-run with it). Returns False only if it's ALREADY running.

        Deliberately does NOT honor coalescing: "Run now" is an explicit user action that
        must visibly do something, so it claims the job even while a coalesce sibling is in
        flight. Within one daemon the runtime's seed lock still serializes the heavy work
        (the claimed job waits on the lock, then runs), so this can't actually run two
        manifest parses at once — it only promotes this job to run next. (The BEAT still
        coalesces, so the automatic path never claims all N siblings at once.)

        It also FORCES the run through the runner's skip-unchanged gate: an explicit Run-now
        on an UNCHANGED source (the common case once a project has been seeded once) would
        otherwise flash "running" and complete instantly with no work + no progress — the
        "doesn't seem to start them" bug. The force marker rides on an IN-MEMORY copy only,
        so the persisted job keeps its real payload and ``complete()`` re-reads it clean."""
        claimed = self._store.claim(job_id, now=self._clock())
        if claimed is None:
            return False
        forced = claimed.model_copy(update={"payload": {**claimed.payload, FORCE_RUN_KEY: True}})
        return self._spawn(forced, reason=reason)

    def _spawn(self, job: ScheduledJob, *, reason: str) -> bool:
        existing = self._tasks.get(job.job_id)
        if existing is not None and not existing.done():
            return False  # already running in THIS process
        self._tasks[job.job_id] = asyncio.create_task(
            self._run(job, reason=reason), name=f"job-{job.job_id}"
        )
        return True

    async def _run(self, job: ScheduledJob, *, reason: str) -> None:
        self._emit(JobEvent(job.job_id, job.kind, "started", reason=reason))
        heartbeat = asyncio.create_task(self._heartbeat_loop(job.job_id))
        error: str | None = None
        try:
            runner = self._runners.get(job.kind)
            if runner is None:
                raise LookupError(f"no runner registered for job kind {job.kind!r}")
            await runner(job)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("scheduler job %s failed", job.job_id)
        finally:
            heartbeat.cancel()
            # Awaiting the cancelled heartbeat must NOT be allowed to skip complete():
            # if the heartbeat task died with its own (non-Cancelled) exception, awaiting it
            # re-raises that here — and an un-completed job stays "running" until its lease
            # goes stale, then gets reclaimed and DOUBLE-RUN. Swallow everything so the job
            # is always finalized exactly once.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            self._store.complete(job.job_id, now=self._clock(), ok=error is None, error=error)
            self._tasks.pop(job.job_id, None)
            phase: Literal["completed", "failed"] = "completed" if error is None else "failed"
            self._emit(JobEvent(job.job_id, job.kind, phase, reason=reason, error=error))

    async def _heartbeat_loop(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            # A transient store error (e.g. a momentarily locked SQLite) must not kill the
            # heartbeat — that would let the lease lapse and a peer reclaim a job that IS
            # still running. Skip this beat and try again on the next interval.
            with contextlib.suppress(Exception):
                self._store.heartbeat(job_id, now=self._clock())

    def _emit(self, event: JobEvent) -> None:
        for listener in self._listeners:
            with contextlib.suppress(Exception):
                listener(event)


def new_job(
    job_id: str,
    trigger: CronTrigger | IntervalTrigger | OnceTrigger | EventTrigger | RawTrigger,
    *,
    now: datetime,
    plugin: str = "",
    kind: str = "",
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    coalesce_key: str | None = None,
) -> ScheduledJob:
    """Build a ``scheduled`` job with ``next_run_at`` primed from the trigger."""
    return ScheduledJob(
        job_id=job_id,
        plugin=plugin,
        kind=kind,
        trigger=trigger,
        payload=payload or {},
        state="scheduled",
        next_run_at=trigger.next_after(now),
        priority=priority,
        coalesce_key=coalesce_key,
    )


__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "FORCE_RUN_KEY",
    "CronTrigger",
    "EventTrigger",
    "IntervalTrigger",
    "JobEvent",
    "JobEventListener",
    "JobLease",
    "JobRunner",
    "OnceTrigger",
    "RawTrigger",
    "ScheduledJob",
    "Scheduler",
    "SchedulerStore",
    "Trigger",
    "new_job",
]
