"""The CLI/TUI runs the scheduler beat in-process — the CLI is just another frontend.

In the daemon a server-level beat ticks every open runtime; the CLI has no such server, so
``HarnessRuntime.start_scheduler_beat()`` runs the same per-runtime tick (``beat_tick``) in
the background. These pin: the beat claims + runs a due job in-process and ``close_all`` stops
it, it's a NO-OP in daemon mode (where the server beat owns ticking, so the two never
double-tick), and a second call is idempotent (one beat per runtime).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alkera_cli.harness import HarnessRuntime
from alkera_cli.plugins.plugin_base.scheduler import IntervalTrigger, new_job
from alkera_core.project import ProjectDirectory


def _stub_registration(runtime: HarnessRuntime, monkeypatch: Any) -> None:
    """Isolate the beat LOOP from the real KB/connection seeds it would otherwise register."""
    monkeypatch.setattr(runtime, "schedule_context_jobs", list)

    async def _noop_refresh(*_a: object, **_k: object) -> list[str]:
        return []

    monkeypatch.setattr(runtime, "schedule_refresh", _noop_refresh)


async def test_start_scheduler_beat_runs_due_jobs_in_process(
    tmp_path: Path, monkeypatch: Any
) -> None:
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"))  # CLI: subprocess_seed=False
    _stub_registration(runtime, monkeypatch)

    ran: list[str] = []

    async def _runner(job: Any) -> None:
        ran.append(job.job_id)

    sched = runtime.scheduler()
    sched.register_runner("work", _runner)
    job = new_job("probe", IntervalTrigger(seconds=3600), now=datetime.now(UTC), kind="work")
    job.next_run_at = datetime.now(UTC)  # due now
    sched.register(job)

    runtime.start_scheduler_beat(interval_seconds=0.02)
    assert runtime._beat_task is not None
    try:
        for _ in range(200):  # up to ~4s — the beat claims + runs the due job within a tick or two
            if ran:
                break
            await asyncio.sleep(0.02)
        assert ran == ["probe"]
    finally:
        await runtime.close_all()
    assert runtime._beat_task is None  # close_all stopped the beat


async def test_start_scheduler_beat_is_a_noop_in_daemon_mode(tmp_path: Path) -> None:
    # The daemon's server-level beat already ticks every runtime, so a per-runtime beat would
    # double-tick: start_scheduler_beat must spawn nothing in daemon mode.
    daemon = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"), subprocess_seed=True)
    daemon.start_scheduler_beat()
    assert daemon._beat_task is None


async def test_start_scheduler_beat_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    runtime = HarnessRuntime(ProjectDirectory(tmp_path / ".alkera"))
    _stub_registration(runtime, monkeypatch)
    runtime.start_scheduler_beat(interval_seconds=0.05)
    first = runtime._beat_task
    runtime.start_scheduler_beat(interval_seconds=0.05)  # second call → no second beat
    assert runtime._beat_task is first
    await runtime.close_all()
