"""Wire blob garbage collection onto the daemon scheduler.

Blobs are content-addressed scratch (spilled results + attachments). They were
never reclaimed — `ChatStore.gc()` existed but had no caller. This registers a
periodic ``blob_gc`` job that mark-and-sweeps unreferenced blobs (everything not
referenced by any chat's `chat.jsonl`, past the grace period), sharing the same
daemon scheduler as the KB jobs (one scheduler, many consumers).

The sweep holds the blob store's own GC lock, so two daemons can't double-delete,
and the reference scan counts spilled-result handles — so a result still visible
in chat history is never swept out from under the user.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base.scheduler import JobRunner, Scheduler

logger = logging.getLogger(__name__)

BLOB_GC_KIND = "blob_gc"
_DEFAULT_BLOB_GC_CADENCE_SECONDS = 6 * 3600  # every 6h; blobs have a 24h grace


def make_blob_gc_runner(project: ProjectDirectory) -> JobRunner:
    from alkera_cli.plugins.plugin_base.scheduler import ScheduledJob

    async def _run(_job: ScheduledJob) -> None:
        # Off the event loop — the sweep scans every chat's JSONL + stats the blob
        # tree (I/O-bound); keep the daemon's JSON-RPC + beat responsive.
        report = await asyncio.to_thread(project.chats().gc)
        if report.deleted:
            logger.info(
                "blob gc: scanned=%d kept=%d deleted=%d freed=%d bytes",
                report.scanned,
                report.kept,
                report.deleted,
                report.freed_bytes,
            )

    return _run


def schedule_blob_gc(scheduler: Scheduler, project: ProjectDirectory, *, now: datetime) -> str:
    """Register the periodic ``blob_gc`` job + its runner. Idempotent — returns the
    job id. NOT primed due-now (a sweep on every project open is wasteful); the
    first interval fires it."""
    from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger, new_job

    scheduler.register_runner(BLOB_GC_KIND, make_blob_gc_runner(project))
    job = new_job(
        BLOB_GC_KIND,
        IntervalTrigger(seconds=float(_DEFAULT_BLOB_GC_CADENCE_SECONDS)),
        now=now,
        kind=BLOB_GC_KIND,
    )
    scheduler.register(job)
    return job.job_id


__all__ = ["BLOB_GC_KIND", "make_blob_gc_runner", "schedule_blob_gc"]
