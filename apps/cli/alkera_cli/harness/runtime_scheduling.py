"""The scheduling half of `HarnessRuntime`: the standing background jobs and the
per-connection refresh/sync jobs.

Split from `runtime.py` so the job machinery reads apart from the session and
permission machinery. `standing_jobs()` is the one source for the standing-job
set: the platform's own (blob reclamation, the cloud-sync lanes) followed by the
ones a distribution registers on `HARNESS_STANDING_JOBS`. `ensure_context_runners`
binds runners from it and the registry-init orphan sweep protects it (through
`protected_kinds()`), so the two sites cannot drift. A kind missing from the sweep's
protected set would be pruned as a phantom orphan by every seed/context worker,
since a worker is a fresh runtime that never registers these runners.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from alkera_cli.harness.extension_points import (
    HARNESS_CONNECTION_JOBS,
    HARNESS_STANDING_JOBS,
    JobContext,
    JobNudge,
    StandingJob,
    workspace_seeder,
)

if TYPE_CHECKING:
    import asyncio

    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base import PluginRegistry
    from alkera_cli.plugins.plugin_base.scheduler import ScheduledJob, Scheduler

logger = logging.getLogger(__name__)


def _platform_jobs(announce: Callable[[str, bool], None]) -> tuple[StandingJob, ...]:
    """The platform's own standing jobs: unreferenced blobs are reclaimed (spilled
    results from deleted chats, orphaned attachments), and the cloud-sync lanes run.
    Each lane re-checks its own gates at run time. The connections lane changes team
    rows on its own schedule, so it is handed the runtime's announcer: an admin's
    change reaches an open editor without it having to ask."""
    from alkera_cli.cloud_sync import job as cloud_sync
    from alkera_cli.plugins.plugin_base import blob_gc_job as blob_gc

    return (
        StandingJob(
            kind=blob_gc.BLOB_GC_KIND,
            runner=lambda ctx: blob_gc.make_blob_gc_runner(ctx.project),
            arm=lambda ctx: blob_gc.schedule_blob_gc(ctx.scheduler, ctx.project, now=ctx.now),
        ),
        StandingJob(
            kind=cloud_sync.CLOUD_SYNC_KIND,
            runner=lambda ctx: cloud_sync.make_cloud_sync_runner(ctx.project, announce=announce),
            arm=lambda ctx: cloud_sync.schedule_cloud_sync(ctx.scheduler, ctx.project, now=ctx.now),
        ),
    )


def standing_jobs(announce: Callable[[str, bool], None]) -> tuple[StandingJob, ...]:
    """Every standing job a runtime binds and arms: the platform's own, then each
    one a distribution registered, in registration order."""
    return (*_platform_jobs(announce), *HARNESS_STANDING_JOBS.items())


def protected_kinds() -> frozenset[str]:
    """The kinds the orphan sweep keeps. A standing job binds its runner on the
    context-jobs path (or in another process), and a per-connection family binds
    on the refresh path, so a missing runner here is no proof of an orphan. A
    knowledge job holds the only record that its connection has ever synced:
    pruning one would re-register it as never run, and a never-run job is armed
    for an immediate pass."""
    return frozenset(
        {
            *(job.kind for job in standing_jobs(lambda _record_id, _removed: None)),
            *(job.kind for job in HARNESS_CONNECTION_JOBS.items()),
        }
    )


class _SchedulingMixin:
    """Job registration + runner binding for `HarnessRuntime`, which mixes this
    in and owns everything these methods share: the scheduler, the project, the
    seed/column locks, and the subprocess spawners."""

    if TYPE_CHECKING:
        _project: ProjectDirectory
        _seed_lock: asyncio.Lock
        _lineage_column_lock: asyncio.Lock
        _subprocess_seed: bool

        def scheduler(self) -> Scheduler: ...
        async def plugin_registry(self) -> PluginRegistry: ...
        async def _spawn_seed_subprocess(
            self,
            plugin: str,
            conn_handle: str,
            job_id: str,
            forced: bool = False,
            metered: bool = False,
        ) -> None: ...
        async def _spawn_column_subprocess(self, job_id: str) -> None: ...
        async def _spawn_kb_subprocess(self, job_id: str, forced: bool = False) -> None: ...
        def announce_team_connection(self, record_id: str, *, removed: bool = False) -> None: ...

    def _announce_synced_row(self, record_id: str, removed: bool) -> None:
        """One row the connections lane changed, in the shape the lane speaks."""
        self.announce_team_connection(record_id, removed=removed)

    def _job_context(self) -> JobContext:
        """What a job binds and arms with on this runtime. In daemon mode the heavy
        knowledge index and column drain run OUT of the daemon's interpreter (never
        freezing it); the CLI keeps them in-process."""
        return JobContext(
            project=self._project,
            scheduler=self.scheduler(),
            now=datetime.now(UTC),
            kb_worker=self._spawn_kb_subprocess if self._subprocess_seed else None,
            column_worker=self._spawn_column_subprocess if self._subprocess_seed else None,
            column_lock=self._lineage_column_lock,
        )

    def nudge_standing_jobs(self, event: JobNudge) -> None:
        """Bring forward every standing job ``event`` concerns. ``reschedule_soon``,
        not ``prime_due``: a recurring job already ran on open, and ``prime_due`` only
        arms a never-run job, so a change mid-session would otherwise wait out the
        cadence."""
        scheduler = self.scheduler()
        for job in HARNESS_STANDING_JOBS.items():
            if event in job.nudged_on:
                scheduler.reschedule_soon(job.kind)

    async def schedule_refresh(self, registry: PluginRegistry | None = None) -> list[str]:
        """Register every connection's background jobs + their runners: the refresh job
        for a connection whose plugin declares a ``RefreshProvider`` (the
        context+lineage feed), and each registered per-connection family (the document
        sync of a connection whose plugin reads documents). They are per-connection work
        armed on the same events -- an open, an add, a re-discovery -- so they register
        together rather than leaving one to a caller that remembers. Idempotent; returns
        the job_ids touched. Pass ``registry`` to reuse an already-resolved one (so
        callers inside the registry-init lock don't re-enter ``plugin_registry()``)."""
        reg = registry if registry is not None else await self.plugin_registry()
        # Share the runtime's seed lock so the refresh beat serializes against the
        # activation seed (one seed at a time per project -- never N concurrent parses).
        # In daemon mode, hand the runner a subprocess executor so the heavy seed runs
        # OUT of the daemon's interpreter (never freezes it); the CLI keeps it in-process.
        seed_subprocess = self._spawn_seed_subprocess if self._subprocess_seed else None
        seeder = workspace_seeder()
        job_ids: list[str] = []
        if seeder is not None:
            job_ids = seeder.schedule_refresh(
                reg,
                self.scheduler(),
                self._project,
                now=datetime.now(UTC),
                seed_lock=self._seed_lock,
                seed_subprocess=seed_subprocess,
                # A table refresh finished -> kick the GLOBAL column drain, but only once ALL
                # table refreshes are done (see _nudge_column_drain) so it starts against the
                # complete relation_sql set instead of a moving partial count.
                on_seeded=self._nudge_column_drain,
            )
        # A per-connection family that can't register its jobs must not cost the lineage
        # ones theirs -- those are already registered above and stay registered.
        for family in HARNESS_CONNECTION_JOBS.items():
            with contextlib.suppress(Exception):
                job_ids.extend(family.arm(reg, self._job_context()))
        return job_ids

    def _all_table_refreshes_done(self, *, exclude_job_id: str | None = None) -> bool:
        """True when every per-connection table-lineage refresh has COMPLETED -- none currently
        running and none scheduled-and-due. The per-connection refresh jobs are exactly the ones
        carrying a ``connection`` in their payload (the column singleton + the KB jobs don't), so
        that's the filter -- minus every registered per-connection family (the knowledge sync),
        which also names a connection but emits no lineage, and whose hours-long pass over a
        document corpus would otherwise hold the column drain back for as long as it ran.
        ``exclude_job_id`` skips the refresh that's calling this from inside its own run (it's
        still ``running`` until the scheduler finalizes it)."""
        families = {family.kind for family in HARNESS_CONNECTION_JOBS.items()}
        now = datetime.now(UTC)
        for job in self.scheduler().list_jobs():
            if job.job_id == exclude_job_id or not job.payload.get("connection"):
                continue
            if job.kind in families:
                continue
            if job.state == "running":
                return False
            if job.state == "scheduled" and job.next_run_at is not None and job.next_run_at <= now:
                return False  # due (e.g. a coalesced sibling waiting its turn) -> more to come
        return True

    def _nudge_column_drain(self, completed: ScheduledJob) -> None:
        """A table-lineage refresh finished -> kick the GLOBAL column drain, but ONLY once ALL the
        per-connection table refreshes are done (none running or due). So the column drain starts
        against the COMPLETE ``relation_sql`` set -- a stable, correct total -- instead of a moving
        partial count while sibling table jobs are still emitting. The LAST refresh in a coalesced
        batch fires this; earlier ones see a sibling still pending and skip. (Each refresh commits
        its ``relation_sql`` before its subprocess exits, so 'all done' means SQLite has
        caught up.)"""
        with contextlib.suppress(Exception):
            if self._all_table_refreshes_done(exclude_job_id=completed.job_id):
                self.nudge_standing_jobs("table_lineage_settled")

    def ensure_context_runners(self) -> None:
        """Idempotently (re)register the standing background job RUNNERS on the scheduler --
        runner binding only, NO job arming (so it never re-seeds). The daemon beat calls this
        each tick so a one-off registration miss self-heals: a persisted kb_* / lineage_column
        job whose runner failed to register would otherwise be skipped forever (the beat
        declines to run a kind with no runner). The common path is a cheap set-subset check +
        return."""
        sched = self.scheduler()
        jobs = standing_jobs(self._announce_synced_row)
        if {job.kind for job in jobs} <= sched.registered_kinds():
            return  # all bound already (the steady state)
        ctx = self._job_context()
        for job in jobs:
            sched.register_runner(job.kind, job.runner(ctx))

    def schedule_context_jobs(self) -> list[str]:
        """Register the project's standing jobs so the KB self-seeds and stays bounded,
        unreferenced blobs are reclaimed and the cloud-sync lanes run the moment a
        project opens -- no manual ``alkera context seed`` needed. The scheduler beat
        then runs them. Best-effort per job (a single registration failure never blocks
        the open) and dedup-by-kind. Returns the job ids registered.

        The cloud-sync lanes (KB pull + KB push) are registered unconditionally
        now that the two-level sync gate exists: each lane re-checks the
        effective gate (org master AND per-project switch) at RUN time and
        no-ops when it's off or the user is logged out -- so a scratch project
        or a sync-disabled org costs nothing, and flipping a switch takes
        effect on the next beat without re-registration."""
        jobs: list[str] = []
        for job in standing_jobs(self._announce_synced_row):
            try:
                jobs.append(job.arm(self._job_context()))
            except Exception:
                logger.warning("context.schedule_jobs.failed", exc_info=True)
        return jobs
