"""Daemon JSON-RPC surface for the scheduler.

Drives the handlers directly with a minimal fake server (``_runtime_for`` only
needs a settable object). Proves list/run_now/cancel + that a run emits
``scheduler.job_event`` notifications the editor can render.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alkera_cli.daemon.methods.scheduler import (
    SchedulerCancelRequest,
    SchedulerListRequest,
    SchedulerRunNowRequest,
    _scheduler_for,
    _start_scheduler_beat,
    _stop_scheduler_beat,
    scheduler_cancel,
    scheduler_list,
    scheduler_run_now,
)
from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger, new_job


def _fake_server() -> Any:
    notifications: list[tuple[str, Any]] = []

    async def _notify(method: str, params: Any = None) -> None:
        notifications.append((method, params))

    return SimpleNamespace(notify=_notify, _notifications=notifications)


async def _wait_for(cond: Any, *, budget: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


async def test_daemon_schedules_refresh_jobs_on_first_touch_without_plugin_list(
    tmp_path: Path,
) -> None:
    # The connector REFRESH jobs (dbt_refresh) must be scheduled the moment the daemon
    # first touches the project — via ANY RPC that builds the runtime — NOT only after
    # the user opens a chat or the Plugins & Connections page (which calls plugin.list).
    # Here we touch the project ONLY through _runtime_for (as scheduler.list / auth would)
    # and never call plugin.list.
    import json

    from alkera_cli.app.runtime_pool import runtime_for as _runtime_for

    (tmp_path / "dbt_project.yml").write_text("name: shop\nprofile: shop\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {"adapter_type": "duckdb"},
                "nodes": {
                    "model.shop.stg": {
                        "resource_type": "model",
                        "name": "stg",
                        "database": "db",
                        "schema": "main",
                        "relation_name": "db.main.stg",
                        "depends_on": {"nodes": []},
                    }
                },
            }
        )
    )
    srv = _fake_server()
    rt = _runtime_for(srv, str(tmp_path))  # the daemon's first touch — kicks the eager build
    # Drain the background registry build the touch kicked.
    await asyncio.gather(*list(getattr(srv, "_eager_registry_tasks", set())))
    await _wait_for(lambda: any(j.kind == "dbt_refresh" for j in rt.scheduler().list_jobs()))
    job = next(j for j in rt.scheduler().list_jobs() if j.kind == "dbt_refresh")
    assert job.next_run_at is not None  # primed due-now, so the beat starts it immediately


async def test_scheduler_list_returns_registered_jobs(tmp_path: Path) -> None:
    srv = _fake_server()
    pp = str(tmp_path)
    _scheduler_for(srv, pp).register(
        new_job(
            "j",
            IntervalTrigger(seconds=60),
            now=datetime.now(UTC),
            kind="work",
            plugin="snow",
            payload={"connection": "dbt_dex"},
            coalesce_key="dbt_refresh",
        )
    )
    resp = await scheduler_list(srv, SchedulerListRequest(project_path=pp))
    # Opening a project also registers the standing KB jobs (kb_seed/kb_prune), so
    # assert about the job we registered rather than the exact list.
    job = next(j for j in resp.jobs if j.job_id == "j")
    assert job.plugin == "snow"
    assert job.trigger["type"] == "interval"
    # The connection + coalesce_key are surfaced so the UI can tell N same-kind jobs apart
    # and show a deferred sibling as "Queued".
    assert job.connection == "dbt_dex"
    assert job.coalesce_key == "dbt_refresh"


async def test_run_now_runs_and_emits_job_events(tmp_path: Path) -> None:
    srv = _fake_server()
    pp = str(tmp_path)
    sched = _scheduler_for(srv, pp)
    calls: list[str] = []

    async def _runner(job: Any) -> None:
        calls.append(job.job_id)

    sched.register_runner("work", _runner)
    sched.register(new_job("j", IntervalTrigger(seconds=60), now=datetime.now(UTC), kind="work"))

    resp = await scheduler_run_now(
        srv, SchedulerRunNowRequest(project_path=pp, job_id="j", reason="manual")
    )
    assert resp.started is True
    await sched._tasks["j"]  # the run task spawned by run_now
    await _wait_for(lambda: len(srv._notifications) == 2)  # started + completed flush

    assert calls == ["j"]
    assert [m for m, _ in srv._notifications] == ["scheduler.job_event", "scheduler.job_event"]
    assert [p.phase for _, p in srv._notifications] == ["started", "completed"]
    assert all(p.reason == "manual" for _, p in srv._notifications)


async def test_scheduler_beat_ticks_and_runs_due_jobs(tmp_path: Path) -> None:
    """The PERIODIC beat (the daemon startup hook, not just the run_now RPC) actually
    claims + runs a DUE job. Pins the full wire: serve()'s startup hooks → the 1s
    `_loop()` → `tick()` → atomic claim → run, for every open runtime. Without this
    every scheduled job (seed/prune/sync/push) would be registered but never fire."""
    pp = str(tmp_path)
    srv = _fake_server()
    sched = _scheduler_for(srv, pp)  # populates srv.harness_runtimes + standing jobs
    # Drop the standing KB jobs so this test is hermetic (no real seed/sync/network).
    for j in list(sched.list_jobs()):
        sched.remove(j.job_id)

    calls: list[str] = []

    async def _runner(job: Any) -> None:
        calls.append(job.job_id)

    sched.register_runner("work", _runner)
    job = new_job("beatjob", IntervalTrigger(seconds=3600), now=datetime.now(UTC), kind="work")
    job.next_run_at = datetime.now(UTC)  # DUE now → the next beat must claim + run it
    sched.register(job)

    await _start_scheduler_beat(srv)  # the daemon startup hook spawns the 1s beat
    try:
        # The first tick lands about 1.1 s in: the beat sleeps a full second
        # before it, then the first tick lazily imports the standing runners.
        # The wait returns the moment the job runs; the wide budget only covers
        # a loaded runner, where those imports have taken over 3 s.
        await _wait_for(lambda: "beatjob" in calls, budget=30.0)
    finally:
        await _stop_scheduler_beat(srv)
    assert calls == ["beatjob"]


async def test_run_now_unknown_job_is_not_started(tmp_path: Path) -> None:
    srv = _fake_server()
    resp = await scheduler_run_now(
        srv, SchedulerRunNowRequest(project_path=str(tmp_path), job_id="nope")
    )
    assert resp.started is False


async def test_cancel_stops_the_run_but_never_deletes_the_job(tmp_path: Path) -> None:
    # The scheduler.cancel RPC STOPS an in-flight run; it NEVER deletes the job — jobs are
    # permanent, there is no remove affordance. A non-running job has nothing to stop
    # (stopped=False), but it stays listed regardless.
    srv = _fake_server()
    pp = str(tmp_path)
    _scheduler_for(srv, pp).register(
        new_job("j", IntervalTrigger(seconds=60), now=datetime.now(UTC), kind="work")
    )
    resp = await scheduler_cancel(srv, SchedulerCancelRequest(project_path=pp, job_id="j"))
    assert resp.stopped is False  # nothing running to stop
    # "j" is STILL listed — cancel keeps the job (it would reschedule if it had been running).
    remaining = (await scheduler_list(srv, SchedulerListRequest(project_path=pp))).jobs
    assert "j" in [j.job_id for j in remaining]
