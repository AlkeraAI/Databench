"""The generic ``alkera_cloud_sync`` scheduler job: ONE kind, one runner,
one job per LANE.

A lane is one cloud-to-workspace sync surface (team knowledge, a team's
preconfigured connections, their health, their inventory). The open platform
owns the engine and none of the lanes: each lane is a :class:`SyncLane`
registered into :data:`CLOUD_SYNC_LANES` by the distribution that ships it, so
a new surface plugs in as a lane instead of growing another job kind, and the
open platform on its own runs none. Each lane is its own *scheduled job
instance* (``alkera_cloud_sync:<lane>``) so the existing scheduler machinery
keeps doing the heavy lifting per lane: its own cadence, its own lease (two
daemons can't run the same lane concurrently), its own failure surface in the
Jobs UI, and crash recovery.

Stale old-kind job files are swept by ``prune_orphans``, and the lanes retired
by name are dropped by :func:`schedule_cloud_sync`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from alkera_core.extensions import ExtensionError, ExtensionPoint

if TYPE_CHECKING:
    from datetime import datetime

    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base.scheduler import (
        JobRunner,
        ScheduledJob,
        Scheduler,
    )

logger = structlog.get_logger(__name__)

CLOUD_SYNC_KIND = "alkera_cloud_sync"

RowAnnouncer = Callable[[str, bool], None]
"""``(record_id, removed)`` for one row a lane pass changed.

Sync is the only writer that changes a row without anybody asking it to (an
admin rotates a credential, adds a connection, takes one away), so it is the
one path where a surface has no reason to re-read. Announcing what it changed is
what turns that into a row that updates on its own. ``removed`` distinguishes a
row that is gone (the editor has to drop it) from one that merely moved."""


@dataclass(frozen=True, slots=True)
class LaneRun:
    """What one lane pass is given: the project it syncs into, and the
    runtime's row announcer (``None`` on the CLI beat, where nobody watches)."""

    project: ProjectDirectory
    announce: RowAnnouncer | None = None


@dataclass(frozen=True, slots=True)
class SyncLane:
    """One cloud-to-workspace sync surface.

    ``due_on_open`` primes the lane's first run on every open: a lane that
    delivers what a member is waiting on runs at once, a maintenance lane waits
    one cadence. ``after_sign_in`` makes a sign-in or sign-out due it on the next
    beat, for a lane whose answer depends on who is signed in."""

    name: str
    cadence_seconds: float
    due_on_open: bool
    run: Callable[[LaneRun], Awaitable[None]]
    after_sign_in: bool = False


#: The lanes the cloud-sync job runs, in registration order. Empty on the open
#: platform alone.
CLOUD_SYNC_LANES: ExtensionPoint[SyncLane] = ExtensionPoint("cloud_sync_lanes")


def registered_lanes(point: ExtensionPoint[SyncLane] = CLOUD_SYNC_LANES) -> dict[str, SyncLane]:
    """Every registered lane by name. Two lanes under one name would share one
    job id, so that is refused rather than one silently shadowing the other."""
    lanes: dict[str, SyncLane] = {}
    for lane in point.items():
        if lane.name in lanes:
            raise ExtensionError(f"two cloud-sync lanes are named {lane.name!r}")
        lanes[lane.name] = lane
    return lanes


def lane_job_id(lane: str) -> str:
    return f"{CLOUD_SYNC_KIND}:{lane}"


def make_cloud_sync_runner(
    project: ProjectDirectory,
    *,
    announce: RowAnnouncer | None = None,
    point: ExtensionPoint[SyncLane] = CLOUD_SYNC_LANES,
) -> JobRunner:
    """One runner for every lane; the job's payload names which lane runs.

    ``announce`` is the runtime's row announcer, handed to every lane pass. The
    CLI beat passes none."""

    async def _run(job: ScheduledJob) -> None:
        name = str(job.payload.get("lane", ""))
        lane = registered_lanes(point).get(name)
        if lane is None:
            # An unknown lane (a downgrade racing a newer daemon's job file, or a
            # lane this build does not ship) must not crash the beat: log it and
            # let whichever build knows the lane own it.
            logger.warning("cloud_sync.unknown_lane", lane=name, job_id=job.job_id)
            return
        await lane.run(LaneRun(project=project, announce=announce))

    return _run


def _is_current_registration(job: ScheduledJob, lane: str, cadence_seconds: float) -> bool:
    """Whether a persisted job already IS this lane's registration — same kind, same
    lane, same cadence. Anything else is a stale shape that has to be replaced."""
    from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger

    return (
        job.kind == CLOUD_SYNC_KIND
        and job.payload.get("lane") == lane
        and isinstance(job.trigger, IntervalTrigger)
        and job.trigger.seconds == cadence_seconds
    )


def _register_lane(
    scheduler: Scheduler,
    lane: str,
    *,
    now: datetime,
    cadence_seconds: float,
    due_now: bool,
) -> str:
    from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger, new_job

    job_id = lane_job_id(lane)
    existing = next((j for j in scheduler.list_jobs() if j.job_id == job_id), None)
    if existing is not None and existing.state == "running":
        return job_id
    if existing is not None and _is_current_registration(existing, lane, cadence_seconds):
        # KEEP the job so its pending countdown survives. Registration runs on every
        # daemon first-open and CLI beat start, and re-creating the job restarted the
        # clock — a user who reopens their editor more often than the hourly push
        # cadence would never once reach a push.
        if due_now and scheduler.prime_due(job_id, now=now) is None:
            scheduler.reschedule_soon(job_id, now=now)
        return job_id
    if existing is not None:
        scheduler.remove(job_id)  # replace a stale registration (migrate trigger/cadence)
    job = new_job(
        job_id,
        IntervalTrigger(seconds=cadence_seconds),
        now=now,
        kind=CLOUD_SYNC_KIND,
        payload={"lane": lane},
    )
    if due_now:
        job.next_run_at = now  # first run on this open, then every cadence
    scheduler.register(job)
    return job_id


#: Lanes this kind once ran, retired BY NAME. The sweep deletes exactly these ids:
#: a lane only a newer build knows is that build's to run, and deleting anything
#: unrecognized let an older CLI sharing the project directory starve a newer
#: daemon's lane a full cadence on every open.
_RETIRED_LANES = ("kb_push",)


def _retire_removed_lanes(scheduler: Scheduler) -> None:
    """Drop the persisted jobs of the lanes retired by name. The job kind stays
    protected, so ``prune_orphans`` cannot reach them — without this an upgraded
    client keeps firing a retired job on its old cadence forever, answered only by
    the unknown-lane warning."""
    retired = {lane_job_id(lane) for lane in _RETIRED_LANES}
    for job in scheduler.list_jobs():
        if job.job_id in retired and job.state != "running":
            scheduler.remove(job.job_id)


def schedule_cloud_sync(
    scheduler: Scheduler,
    project: ProjectDirectory,
    *,
    now: datetime,
    point: ExtensionPoint[SyncLane] = CLOUD_SYNC_LANES,
) -> str:
    """Register the cloud-sync runner + every registered lane's job, and retire
    the lane jobs retired by name. Idempotent."""
    scheduler.register_runner(CLOUD_SYNC_KIND, make_cloud_sync_runner(project, point=point))
    for lane in registered_lanes(point).values():
        _register_lane(
            scheduler,
            lane.name,
            now=now,
            cadence_seconds=lane.cadence_seconds,
            due_now=lane.due_on_open,
        )
    _retire_removed_lanes(scheduler)
    return CLOUD_SYNC_KIND


def nudge_after_sign_in(
    scheduler: Scheduler, *, point: ExtensionPoint[SyncLane] = CLOUD_SYNC_LANES
) -> None:
    """Make every lane that follows the sign-in due on the next beat, so a login
    or logout reconciles within seconds instead of a cadence. Gated, not
    forced: a signed-out lane still no-ops cheaply."""
    for lane in registered_lanes(point).values():
        if lane.after_sign_in:
            scheduler.reschedule_soon(lane_job_id(lane.name))


__all__ = [
    "CLOUD_SYNC_KIND",
    "CLOUD_SYNC_LANES",
    "LaneRun",
    "RowAnnouncer",
    "SyncLane",
    "lane_job_id",
    "make_cloud_sync_runner",
    "nudge_after_sign_in",
    "registered_lanes",
    "schedule_cloud_sync",
]
