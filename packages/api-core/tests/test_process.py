"""Cross-platform process liveness + termination.

These run against REAL child processes on every platform — the POSIX hosts
exercise the signal-0 path and the Windows CI job runs the same suite through
the Win32 ctypes branch. The key invariant pinned here is that
`process_alive` only *reports*, never *kills* — the bug it replaces was an
`os.kill(pid, 0)` probe that terminates the target on Windows.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest
from alkera_core.process import (
    kill_process,
    kill_tree_async,
    kill_tree_now,
    process_alive,
    process_start_id,
    terminate_process,
)


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    """A real child that sleeps long enough to probe + terminate."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _wait_dead(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("pid", [0, -1, -12345])
def test_process_alive_rejects_nonpositive_pids(pid: int) -> None:
    assert process_alive(pid) is False


def test_process_alive_true_for_self() -> None:
    import os

    assert process_alive(os.getpid()) is True


def test_process_alive_false_for_reaped_child() -> None:
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    assert process_alive(proc.pid) is False


def test_process_alive_does_not_kill_the_target() -> None:
    """The whole point of the rewrite: probing must NOT terminate the process
    (the old `os.kill(pid, 0)` idiom does on Windows)."""
    proc = _spawn_sleeper()
    try:
        for _ in range(5):
            assert process_alive(proc.pid) is True
        # Still running after repeated probes.
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_terminate_process_ends_a_real_child() -> None:
    proc = _spawn_sleeper()
    try:
        terminate_process(proc.pid)
        assert _wait_dead(proc), "terminate_process did not stop the child"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_process_ends_a_real_child() -> None:
    proc = _spawn_sleeper()
    try:
        kill_process(proc.pid)
        assert _wait_dead(proc), "kill_process did not stop the child"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize(
    "fn",
    [terminate_process, kill_process, process_alive, kill_tree_now],
)
def test_best_effort_on_dead_pid_never_raises(fn: object) -> None:
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    # A second call against a now-dead pid must be a quiet no-op / False.
    assert fn(proc.pid) in (None, False)  # type: ignore[operator]


# --- process_start_id: which incarnation of a pid ---------------------------


@pytest.mark.parametrize("pid", [0, -1, -12345])
def test_process_start_id_is_none_for_nonpositive_pids(pid: int) -> None:
    assert process_start_id(pid) is None


def test_process_start_id_is_the_same_on_every_read_of_a_live_process() -> None:
    """The stamp identifies the process, so it cannot drift between two reads —
    a lock holder records it once and every later prober must see that value."""
    import os

    own = process_start_id(os.getpid())
    assert own is not None
    assert process_start_id(os.getpid()) == own
    proc = _spawn_sleeper()
    try:
        first = process_start_id(proc.pid)
        assert first is not None
        time.sleep(0.05)
        assert process_start_id(proc.pid) == first
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_process_start_id_tells_two_live_processes_apart() -> None:
    """Two processes alive at once carry different stamps — the property that
    lets a later process handed a dead holder's pid be told from the holder."""
    import os

    proc = _spawn_sleeper()
    try:
        assert process_start_id(proc.pid) != process_start_id(os.getpid())
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_process_start_id_never_invents_a_new_incarnation_for_a_dead_pid() -> None:
    """Once the child is dead its pid reports nothing, or (Windows, while the
    ``Popen`` handle keeps the exited process readable) the stamp it always
    had — never a stamp that would pass for a different, live process."""
    proc = _spawn_sleeper()
    before = process_start_id(proc.pid)
    assert before is not None
    proc.kill()
    proc.wait(timeout=5)
    assert process_alive(proc.pid) is False
    assert process_start_id(proc.pid) in (None, before)


# --- ending a command and everything it started -------------------------------

_PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(30)\n"
)


def _spawn_tree() -> tuple[subprocess.Popen[bytes], int]:
    """A command leading its own group (a session on POSIX) with a child under
    it; the child's pid, as the command printed it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _PARENT],
        stdout=subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    assert proc.stdout is not None
    return proc, int(proc.stdout.readline())


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_killing_a_tree_now_ends_the_child_and_what_it_started() -> None:
    proc, child = _spawn_tree()
    try:
        assert process_alive(child)
        kill_tree_now(proc.pid)
        assert _wait_dead(proc), "the child outlived its tree's end"
        assert _wait_pid_gone(child), "the child's own child outlived its tree's end"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        kill_process(child)


async def _spawn_tree_async(script: str) -> tuple[asyncio.subprocess.Process, int]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=sys.platform != "win32",
    )
    assert proc.stdout is not None
    return proc, int(await proc.stdout.readline())


@pytest.mark.parametrize("grace", [0, 2.0, None])
async def test_killing_a_tree_async_ends_the_child_and_what_it_started(grace: float | None) -> None:
    proc, child = await _spawn_tree_async(_PARENT)
    try:
        await asyncio.wait_for(kill_tree_async(proc, grace=grace), 15)
        assert proc.returncode is not None
        assert _wait_pid_gone(child), "the child's own child outlived its tree's end"
    finally:
        kill_process(child)


_STUBBORN = (
    "import signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(30)\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals: a child ignoring SIGTERM")
async def test_a_tree_that_ignores_the_ask_is_killed_after_the_grace() -> None:
    proc, child = await _spawn_tree_async(_STUBBORN)
    try:
        await asyncio.wait_for(kill_tree_async(proc, grace=0.3), 15)
        assert proc.returncode == -9
    finally:
        kill_process(child)


@pytest.mark.parametrize("pid", [0, -1])
def test_killing_a_tree_now_ignores_a_pid_that_names_no_process(pid: int) -> None:
    # On POSIX a group signal to 0 or -1 would reach this test's own group or
    # every process it may signal: neither is ever sent, so this test is still
    # here to see it return.
    assert kill_tree_now(pid) is None
