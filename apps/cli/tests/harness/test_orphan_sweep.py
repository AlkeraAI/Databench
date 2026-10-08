"""The agent registry + orphan sweep, with the critical invariant that it reaps
ONLY true orphans — never a live process whose PID was recycled, and never an
agent whose spawning alkera is still alive.

Runs against REAL child processes on the CI's POSIX hosts; the identity check
(process creation time via psutil) is platform-agnostic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alkera_cli.harness import orphan_sweep
from alkera_cli.harness.orphan_sweep import (
    process_create_time,
    read_pid_breadcrumb,
    register_agent,
    same_process,
    sweep_orphaned_agents,
    write_pid_breadcrumb,
)
from alkera_cli.host import paths


@pytest.fixture
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "alkera-home"
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    return home


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _wait_dead(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return False


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=5)
    return p.pid


# --- identity primitives -----------------------------------------------------


def test_same_process_true_for_self() -> None:
    assert same_process(os.getpid(), process_create_time(os.getpid())) is True


def test_same_process_false_when_create_time_unknown() -> None:
    # No recorded identity → we refuse to claim it's the same process.
    assert same_process(os.getpid(), None) is False


def test_same_process_false_on_pid_reuse() -> None:
    """A live PID with a DIFFERENT recorded creation time is a recycled PID —
    not the process we recorded."""
    assert same_process(os.getpid(), 1.0) is False  # bogus old create_time


# --- pid breadcrumb ----------------------------------------------------------


def test_pid_breadcrumb_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "pid"
    write_pid_breadcrumb(path, os.getpid())
    parsed = read_pid_breadcrumb(path)
    assert parsed is not None
    pid, ct = parsed
    assert pid == os.getpid()
    assert ct is not None


def test_pid_breadcrumb_tolerates_legacy_int(tmp_path: Path) -> None:
    path = tmp_path / "pid"
    path.write_text("4242")
    assert read_pid_breadcrumb(path) == (4242, None)


def test_pid_breadcrumb_missing_or_garbage(tmp_path: Path) -> None:
    assert read_pid_breadcrumb(tmp_path / "nope") is None
    (tmp_path / "bad").write_text("not-json-not-int")
    assert read_pid_breadcrumb(tmp_path / "bad") is None


# --- sweep -------------------------------------------------------------------


def test_sweep_reaps_true_orphan(isolate_home: Path) -> None:
    """Agent alive + spawning alkera gone → reaped."""
    agent = _spawn_sleeper()
    try:
        entry = paths.agents_dir() / f"{agent.pid}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            json.dumps(
                {
                    "agent_pid": agent.pid,
                    "agent_create_time": process_create_time(agent.pid),
                    "parent_pid": _dead_pid(),  # spawning alkera is gone
                    "parent_create_time": None,
                    "host": orphan_sweep._hostname(),
                    "pid_file": None,
                }
            )
        )
        assert sweep_orphaned_agents() == 1
        assert _wait_dead(agent), "true orphan was not reaped"
        assert not entry.exists()  # breadcrumb cleaned up
    finally:
        _kill(agent)


def test_sweep_spares_agent_with_live_parent(isolate_home: Path) -> None:
    """Agent alive + spawning alkera (this test process) still alive → spared."""
    agent = _spawn_sleeper()
    try:
        register_agent(agent.pid)  # parent = os.getpid(), which is alive
        assert sweep_orphaned_agents() == 0
        assert agent.poll() is None  # still running
        assert (paths.agents_dir() / f"{agent.pid}.json").exists()
    finally:
        _kill(agent)


def test_sweep_spares_agent_when_parent_unidentifiable_but_alive(isolate_home: Path) -> None:
    """A None recorded parent_create_time means we can't IDENTIFY the parent — NOT that it's
    gone. If the parent PID is still alive (here, this test process), the agent must be SPARED;
    treating unidentifiable-but-alive as 'provably gone' would reap a LIVE parent's agent (the bug
    a psutil hiccup at register time, or a legacy entry without the field, would trigger)."""
    agent = _spawn_sleeper()
    try:
        entry = paths.agents_dir() / f"{agent.pid}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            json.dumps(
                {
                    "agent_pid": agent.pid,
                    "agent_create_time": process_create_time(agent.pid),
                    "parent_pid": os.getpid(),  # the spawning alkera is ALIVE (this test process)
                    "parent_create_time": None,  # ...but its identity wasn't recorded
                    "host": orphan_sweep._hostname(),
                    "pid_file": None,
                }
            )
        )
        assert sweep_orphaned_agents() == 0
        time.sleep(0.2)
        assert agent.poll() is None, "agent of a live (but unidentifiable) parent was reaped!"
        assert entry.exists()  # left intact — the parent is present
    finally:
        _kill(agent)


def test_breadcrumb_and_registry_writes_are_atomic(isolate_home: Path, tmp_path: Path) -> None:
    """The per-chat breadcrumb + central registry entry are written atomically (temp+rename), so a
    SIGKILL mid-write can never leave a TORN file that reads back as None (which would strand a
    true orphan). Proxy assertion: after each write the target is complete + no sibling temp
    turd remains (the rename landed)."""
    bc = tmp_path / "pid"
    write_pid_breadcrumb(bc, os.getpid())
    assert read_pid_breadcrumb(bc) is not None
    assert [p.name for p in tmp_path.iterdir()] == ["pid"]  # no leftover *.tmp sibling

    register_agent(os.getpid())
    agents = paths.agents_dir()
    assert (agents / f"{os.getpid()}.json").exists()
    assert all(not p.name.endswith(".tmp") for p in agents.iterdir())


def test_sweep_does_not_kill_recycled_pid(isolate_home: Path) -> None:
    """The whole point: an entry whose agent_create_time no longer matches the
    live PID is a recycled PID — we must NOT kill that innocent process."""
    victim = _spawn_sleeper()  # stands in for an unrelated process reusing the PID
    try:
        entry = paths.agents_dir() / f"{victim.pid}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            json.dumps(
                {
                    "agent_pid": victim.pid,
                    "agent_create_time": 1.0,  # stale — doesn't match the live proc
                    "parent_pid": _dead_pid(),
                    "parent_create_time": None,
                    "host": orphan_sweep._hostname(),
                    "pid_file": None,
                }
            )
        )
        assert sweep_orphaned_agents() == 0  # nothing reaped
        time.sleep(0.2)
        assert victim.poll() is None, "innocent recycled-PID process was killed!"
        assert not entry.exists()  # stale breadcrumb still cleaned up
    finally:
        _kill(victim)


def test_sweep_drops_breadcrumb_for_dead_agent(isolate_home: Path) -> None:
    dead = _dead_pid()
    entry = paths.agents_dir() / f"{dead}.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        json.dumps(
            {
                "agent_pid": dead,
                "agent_create_time": 123.0,
                "parent_pid": _dead_pid(),
                "parent_create_time": None,
                "host": orphan_sweep._hostname(),
                "pid_file": None,
            }
        )
    )
    assert sweep_orphaned_agents() == 0
    assert not entry.exists()


def test_sweep_skips_other_host(isolate_home: Path) -> None:
    agent = _spawn_sleeper()
    try:
        entry = paths.agents_dir() / f"{agent.pid}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            json.dumps(
                {
                    "agent_pid": agent.pid,
                    "agent_create_time": process_create_time(agent.pid),
                    "parent_pid": _dead_pid(),
                    "parent_create_time": None,
                    "host": "some-other-machine",
                    "pid_file": None,
                }
            )
        )
        assert sweep_orphaned_agents() == 0
        assert agent.poll() is None  # left alone — can't judge another host
        assert entry.exists()  # not ours to clean up
    finally:
        _kill(agent)


def test_sweep_removes_pid_file_when_reaping(isolate_home: Path) -> None:
    agent = _spawn_sleeper()
    pid_file = isolate_home / "chat" / ".runtime" / "pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("whatever")
    try:
        entry = paths.agents_dir() / f"{agent.pid}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            json.dumps(
                {
                    "agent_pid": agent.pid,
                    "agent_create_time": process_create_time(agent.pid),
                    "parent_pid": _dead_pid(),
                    "parent_create_time": None,
                    "host": orphan_sweep._hostname(),
                    "pid_file": str(pid_file),
                }
            )
        )
        assert sweep_orphaned_agents() == 1
        assert _wait_dead(agent)
        assert not pid_file.exists()
    finally:
        _kill(agent)


def test_sweep_empty_registry_is_noop(isolate_home: Path) -> None:
    assert sweep_orphaned_agents() == 0
