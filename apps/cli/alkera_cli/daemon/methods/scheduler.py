"""Daemon JSON-RPC for the scheduler.

Three methods — ``scheduler.list`` / ``scheduler.run_now`` / ``scheduler.cancel``
— plus a ``scheduler.job_event`` notification the daemon forwards from the
engine. The per-second **beat** runs as a startup-hooked background task that
ticks every open runtime's scheduler OFF the JSON-RPC request loop; ``run_now``
goes through the SAME atomic claim, so the two can't double-run a job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from pydantic import Field

from alkera_cli.app.runtime_pool import runtime_for
from alkera_cli.daemon.protocol import _DaemonModel, method, notification
from alkera_cli.daemon.server import register_shutdown_hook, register_startup_hook

if TYPE_CHECKING:
    from alkera_cli.daemon.server import JsonRpcServer
    from alkera_cli.plugins.plugin_base.scheduler import JobEvent, JobEventListener, Scheduler

logger = logging.getLogger(__name__)


class _ProjectScoped(_DaemonModel):
    project_path: str


class JobWire(_DaemonModel):
    """A scheduled job's wire shape (for ``scheduler.list``)."""

    job_id: str
    plugin: str = ""
    kind: str = ""
    #: The connection (e.g. a dbt subproject folder) this job seeds, from its payload —
    #: so the UI can tell N same-kind jobs apart (spellbook's 5 dbt subprojects).
    connection: str | None = None
    #: Jobs sharing a non-None key run one-at-a-time; the UI shows a scheduled job whose
    #: key matches a RUNNING job as "Queued" (waiting), not overdue.
    coalesce_key: str | None = None
    state: str = "scheduled"
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_error: str | None = None
    trigger: dict[str, Any] = Field(default_factory=dict)
    # How the most recent run ended: "complete" (did everything) | "incomplete" (more to
    # do next run) | "failed". With last_message, lets the UI show whether a job finished
    # in full or has remaining work.
    last_outcome: str | None = None
    last_message: str | None = None
    # Live progress for a RUNNING job (merged from the worker's sidecar). ``status_text``
    # is the current step; ``progress_total`` None ⇒ show an INDETERMINATE bar, a
    # current/total pair ⇒ a determinate fraction.
    status_text: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_updated_at: float | None = None


class SchedulerListRequest(_ProjectScoped):
    pass


class SchedulerListResponse(_DaemonModel):
    jobs: list[JobWire] = Field(default_factory=list)


class SchedulerRunNowRequest(_ProjectScoped):
    job_id: str
    reason: str = "run_now"


class SchedulerRunNowResponse(_DaemonModel):
    started: bool


class SchedulerCancelRequest(_ProjectScoped):
    job_id: str


class SchedulerCancelResponse(_DaemonModel):
    #: Whether an in-flight run was stopped. The job itself is NEVER deleted (it reschedules
    #: for its next cadence) — jobs are permanent; there is no remove affordance.
    stopped: bool


@notification("scheduler.job_event")
class SchedulerJobEventNotification(_DaemonModel):
    """A job lifecycle transition (started / completed / failed)."""

    project_path: str
    job_id: str
    kind: str
    phase: str
    reason: str = ""
    error: str | None = None


def _job_wire(job: Any, progress: dict[str, Any] | None = None) -> JobWire:
    prog = progress or {}
    return JobWire(
        job_id=job.job_id,
        plugin=job.plugin,
        kind=job.kind,
        connection=job.payload.get("connection") or None,
        coalesce_key=job.coalesce_key,
        state=job.state,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        last_run_at=job.last_run_at.isoformat() if job.last_run_at else None,
        last_error=job.last_error,
        trigger=job.trigger.model_dump(mode="json"),
        last_outcome=job.last_outcome,
        last_message=job.last_message,
        status_text=prog.get("status_text"),
        progress_current=prog.get("current"),
        progress_total=prog.get("total"),
        progress_updated_at=prog.get("updated_at"),
    )


def _make_forwarder(server: JsonRpcServer, project_path: str) -> JobEventListener:
    def _forward(event: JobEvent) -> None:
        with contextlib.suppress(RuntimeError):  # no running loop → skip (tests)
            asyncio.get_running_loop().create_task(
                server.notify(
                    "scheduler.job_event",
                    SchedulerJobEventNotification(
                        project_path=project_path,
                        job_id=event.job_id,
                        kind=event.kind,
                        phase=event.phase,
                        reason=event.reason,
                        error=event.error,
                    ),
                )
            )

    return _forward


def _scheduler_for(server: JsonRpcServer, project_path: str) -> Scheduler:
    """The runtime's scheduler with the job_event→notification listener attached
    exactly once (so the editor gets live progress)."""
    sched = runtime_for(server, project_path).scheduler()
    wired: set[int] = getattr(server, "_scheduler_wired", None) or set()
    if id(sched) not in wired:
        sched.on_event(_make_forwarder(server, project_path))
        wired.add(id(sched))
        server._scheduler_wired = wired  # type: ignore[attr-defined]
    return sched


@method("scheduler.list")
async def scheduler_list(
    server: JsonRpcServer, params: SchedulerListRequest
) -> SchedulerListResponse:
    sched = _scheduler_for(server, params.project_path)
    return SchedulerListResponse(
        jobs=[_job_wire(j, sched.job_progress(j.job_id)) for j in sched.list_jobs()]
    )


@method("scheduler.run_now")
async def scheduler_run_now(
    server: JsonRpcServer, params: SchedulerRunNowRequest
) -> SchedulerRunNowResponse:
    sched = _scheduler_for(server, params.project_path)
    started = await sched.run_now(params.job_id, reason=params.reason)
    return SchedulerRunNowResponse(started=started)


@method("scheduler.cancel")
async def scheduler_cancel(
    server: JsonRpcServer, params: SchedulerCancelRequest
) -> SchedulerCancelResponse:
    """Stop a job's in-flight run (the user-facing "Cancel"). Does NOT delete the job — it
    reschedules for its next cadence. Jobs are permanent; there is intentionally no remove."""
    sched = _scheduler_for(server, params.project_path)
    return SchedulerCancelResponse(stopped=sched.stop(params.job_id))


# --- the beat ------------------------------------------------------------

#: How often (in 1s beats) to health-check each open project's ``.alkera``. Cheap (a
#: directory glob), so 30s recovers a reset promptly without per-second overhead.
_HEALTH_EVERY_TICKS = 30


async def _heal_if_wiped(server: JsonRpcServer, project_path: str) -> None:
    """Self-heal a project whose ``.alkera`` was deleted out from under the cached
    runtime (a common "reset by deleting .alkera" flow) → re-register the standing
    context jobs AND the per-connection lineage refresh jobs: this re-creates the dir
    tree (the atomic writes ``mkdir`` their parents) AND re-arms ``kb_seed`` plus the
    cloud-sync lanes PLUS each offline connection's lineage refresh to re-populate BOTH
    the KB and the lineage graph on the next beat.

    Keys on the ``.alkera`` DIRECTORY being gone, NOT an empty job list: a job list can be
    legitimately empty while the dir exists (e.g. every connection's refresh job was
    internally removed when its connection went away), so an empty list must NOT trigger a
    spurious re-register. (Jobs are permanent from the UI — "Cancel" stops a run, it never
    deletes — so an empty list is never a user "I removed everything".)"""
    runtime = runtime_for(server, project_path)
    if runtime.project.path.exists():
        return  # .alkera is present (healthy, or jobs deliberately cancelled) — leave it
    logger.info("context.health.reinitializing .alkera for %s", project_path)
    with contextlib.suppress(Exception):
        runtime.schedule_context_jobs()
    # Also re-register the per-connection LINEAGE refresh jobs — schedule_context_jobs only re-arms
    # the standing KB/column/team/blob jobs, so without this the lineage graph for the project's
    # existing offline connections is never rebuilt after a reset-while-open. The cached registry
    # already knows the connections, so this is cheap + idempotent and re-primes offline ones.
    with contextlib.suppress(Exception):
        await runtime.schedule_refresh()


@register_startup_hook
async def _start_scheduler_beat(server: JsonRpcServer) -> None:
    """One supervised task ticks every open runtime's scheduler once a second,
    OFF the request loop. A failed tick is logged, never fatal. Every
    ``_HEALTH_EVERY_TICKS`` beats it also health-checks each project's ``.alkera`` and
    reinitializes (jobs + seed) if it was wiped."""

    async def _loop() -> None:
        ticks = 0
        while True:
            await asyncio.sleep(1.0)
            ticks += 1
            check_health = ticks % _HEALTH_EVERY_TICKS == 0
            runtimes = dict(getattr(server, "harness_runtimes", {}))
            for project_path in runtimes:
                try:
                    if check_health:
                        await _heal_if_wiped(server, project_path)
                    # Ensure the JobEvent→notification listener is wired (idempotent) so live
                    # progress reaches the editor, then run the shared per-runtime tick (which
                    # also self-heals a one-off KB-runner bind miss). beat_tick ticks the SAME
                    # scheduler _scheduler_for just wired.
                    _scheduler_for(server, project_path)
                    await runtime_for(server, project_path).beat_tick()
                except Exception:
                    logger.exception("scheduler beat tick failed for %s", project_path)

    server._scheduler_beat = asyncio.create_task(_loop(), name="scheduler-beat")  # type: ignore[attr-defined]


@register_shutdown_hook
async def _stop_scheduler_beat(server: JsonRpcServer) -> None:
    task = getattr(server, "_scheduler_beat", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


__all__ = [
    "JobWire",
    "SchedulerCancelRequest",
    "SchedulerCancelResponse",
    "SchedulerJobEventNotification",
    "SchedulerListRequest",
    "SchedulerListResponse",
    "SchedulerRunNowRequest",
    "SchedulerRunNowResponse",
    "scheduler_cancel",
    "scheduler_list",
    "scheduler_run_now",
]
