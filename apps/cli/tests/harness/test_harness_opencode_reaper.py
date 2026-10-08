"""Tests for `OpencodeHttpAdapter._reap_orphan_opencode()`.

C3: if a previous alkera process crashed before stopping its opencode
subprocess, the next adapter.start() must terminate (then force-kill) that
zombie before spawning a fresh one — otherwise we'd race two opencode
processes on the same isolated XDG_DATA_HOME and stomp on each
other's SQLite locks.

We use synchronous `subprocess.Popen` here (not `asyncio.create_
subprocess_exec`) on purpose — the test wants `proc.poll()` to nudge
the kernel into reaping zombies. ASYNC220 noqa is justified.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters import opencode_http
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.orphan_sweep import write_pid_breadcrumb


def _make_adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="reap-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


async def test_reaper_no_pid_file_is_noop(tmp_path: Path) -> None:
    """No breadcrumb → no reaping → no error."""
    adapter = _make_adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    assert not (adapter._harness_dir / "pid").exists()

    await adapter._reap_orphan_opencode()
    # Idempotent — still no file.
    assert not (adapter._harness_dir / "pid").exists()


async def test_reaper_dead_pid_unlinks_breadcrumb(tmp_path: Path) -> None:
    """A PID that's already gone (e.g. from a clean shutdown that
    failed to clean up the file) just clears the stale breadcrumb."""
    adapter = _make_adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)

    # Spawn + reap a real subprocess so we get a known-dead PID.
    proc = subprocess.Popen([sys.executable, "-c", ""])  # noqa: ASYNC220
    proc.wait()
    # PID is now reusable but as of NOW, it's dead.
    pid_path = adapter._harness_dir / "pid"
    pid_path.write_text(str(proc.pid))

    await adapter._reap_orphan_opencode()
    assert not pid_path.exists()


async def test_reaper_unparseable_pid_unlinks_breadcrumb(tmp_path: Path) -> None:
    """A garbled pid file is treated as stale — unlinked, no error."""
    adapter = _make_adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    pid_path = adapter._harness_dir / "pid"
    pid_path.write_text("not-a-number")

    await adapter._reap_orphan_opencode()
    assert not pid_path.exists()


async def test_reaper_terminates_live_subprocess(tmp_path: Path) -> None:
    """The canonical happy path: a live subprocess (simulating an
    orphaned opencode) is identity-confirmed and reaped.

    Subtle: pytest is the spawning subprocess's parent, so on terminate
    the kernel keeps a zombie entry until the parent calls wait().
    Without that, the reaper's liveness probe would keep
    succeeding and the loop would run its full 3s before falling back
    to a hard kill. We schedule a concurrent reaper task that polls
    ``proc.poll()`` so the zombie is reaped promptly — that matches
    the production case where the orphan is reparented to init and
    init reaps the moment it exits."""
    adapter = _make_adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)

    # `sleep 30` — a long-running subprocess that honours SIGTERM.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: ASYNC220
    pid_path = adapter._harness_dir / "pid"
    # New breadcrumb format records the creation time so the reaper can
    # confirm identity (a bare-int legacy file is deliberately NOT reaped).
    write_pid_breadcrumb(pid_path, proc.pid)
    assert proc.poll() is None

    stop_reaping = asyncio.Event()

    async def background_reap() -> None:
        while not stop_reaping.is_set():
            proc.poll()  # Triggers wait() once child has exited.
            await asyncio.sleep(0.02)

    bg = asyncio.create_task(background_reap())
    try:
        started = time.monotonic()
        await adapter._reap_orphan_opencode()
        elapsed = time.monotonic() - started
    finally:
        stop_reaping.set()
        await bg

    assert elapsed < 2.0, f"reaper too slow: {elapsed:.2f}s"
    assert proc.poll() is not None, "subprocess still alive after reaper"
    assert not pid_path.exists()


async def test_reaper_force_kills_when_terminate_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the graceful terminate doesn't take within 3s, the reaper escalates
    to a hard kill. We simulate "ignores terminate" by making
    ``terminate_process`` a no-op and assert ``kill_process`` is reached."""
    adapter = _make_adapter(tmp_path)
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # noqa: ASYNC220
    pid_path = adapter._harness_dir / "pid"
    write_pid_breadcrumb(pid_path, proc.pid)

    calls: list[str] = []
    real_kill_process = opencode_http.kill_process

    def fake_terminate(pid: int) -> None:
        calls.append("terminate")  # pretend the target ignored it

    def fake_kill(pid: int) -> None:
        calls.append("kill")
        real_kill_process(pid)

    monkeypatch.setattr(opencode_http, "terminate_process", fake_terminate)
    monkeypatch.setattr(opencode_http, "kill_process", fake_kill)

    # Shorten the polling loop to make the test fast — patch
    # asyncio.sleep to be a no-op for this test's duration.
    real_sleep = asyncio.sleep

    async def fast_sleep(_t: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    await adapter._reap_orphan_opencode()

    # Graceful terminate first, then the hard-kill escalation.
    assert calls == ["terminate", "kill"]

    # Clean up the leftover sleep ourselves.
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    assert not pid_path.exists()
