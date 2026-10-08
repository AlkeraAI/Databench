"""Multi-process tests for the per-chat write lock.

These tests spawn real subprocesses and observe the lock behavior
across process boundaries — which is the whole point of the lock.
They're slower than in-process tests (~100-500ms each) but irreplaceable
for verifying:

- Exactly one process wins the lock; the rest fail fast.
- A holder that exits cleanly releases via `atexit`.
- A holder killed with SIGTERM releases via the signal handler.
- A holder killed with SIGKILL leaves a stale lock; the next acquirer
  reclaims via the PID liveness check.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from alkera_core.process import kill_process, process_alive
from alkera_core.project.chats.chat import ChatNotFoundError
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.locking import LockHeldError


def _run_python(script: str, *args: str, env: dict[str, str] | None = None, **kwargs):
    """Run a one-shot Python subprocess with our installed `alkera_core`
    available. Inherits PYTHONPATH from the test runner."""
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _spawn_python(script: str, *args: str, env: dict[str, str] | None = None):
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_only_one_subprocess_wins_the_lock(tmp_path: Path) -> None:
    """Spawn 5 subprocesses racing to open the same chat. Exactly one
    should hold the lock at any moment; the others MUST be blocked.

    Why this design (no timing assumption about loser speed):

    The winner does NOT release on a fixed timer. It announces the win,
    then holds the lock until the parent observes that EVERY loser has
    provably attempted-and-bounced (each loser stamps ``bounced/<pid>``
    on its ``LockHeldError`` before exiting). Only then does the parent
    drop ``RELEASE``.

    This kills the old flake: a fixed ``sleep(2.0)`` loser-window assumed
    all 4 losers would call ``store.open()`` within 2s of the winner's
    announcement. Under heavy load a CPU-starved loser could attempt
    AFTER the release — find the lock free — and become a false second
    winner. Waiting on the bounce markers makes the winner provably hold
    the lock for the ENTIRE window in which all losers attempt, with no
    dependency on how fast any loser gets scheduled. Two winners now
    means a real lock bug, never a slow runner.

    The winner's own RELEASE deadline (120s) is kept well ABOVE the
    parent's bounce-barrier deadline (60s) so the winner never releases
    early while the parent is still waiting for a straggler.
    """
    store = ProjectDirectory(tmp_path / ".alkera").chats()
    chat = store.create(session_id="race")
    chat.close()  # closing releases the lock

    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    bounced_dir = tmp_path / "bounced"
    bounced_dir.mkdir()
    go_file = tmp_path / "GO"
    winner_file = tmp_path / "winner"
    release_file = tmp_path / "RELEASE"

    script = textwrap.dedent(
        """
        import json, os, sys, time
        from pathlib import Path
        from alkera_core.project.directory import ProjectDirectory
        from alkera_core.project.locking import LockHeldError

        (alkera_path, session_id, go_path, ready_path,
         bounced_dir, winner_path, release_path) = sys.argv[1:8]

        # Signal "ready" + block on the barrier so every child is poised
        # on the acquire call at the same moment.
        Path(ready_path).touch()
        gp = Path(go_path)
        while not gp.exists():
            time.sleep(0.005)

        store = ProjectDirectory(alkera_path).chats()
        try:
            chat = store.open(session_id)
        except LockHeldError as exc:
            # Record the bounce on disk BEFORE exiting so the parent can wait for
            # ALL losers to attempt before releasing the winner (no timing race).
            Path(bounced_dir, str(os.getpid())).write_text("blocked")
            print(json.dumps({"result": "blocked", "exc": str(exc)}), flush=True)
            sys.exit(0)
        try:
            print(json.dumps({"result": "won", "pid": chat._lock.payload["pid"]}), flush=True)
            # Announce the win — NOT a release. Hold the lock until RELEASE.
            Path(winner_path).write_text(str(os.getpid()))
            rp = Path(release_path)
            # Deadline ABOVE the parent's bounce-barrier so we never release
            # early; purely defensive against a dead parent.
            deadline = time.monotonic() + 120.0
            while not rp.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            chat.close()
        """
    )

    n = 5
    procs = [
        _spawn_python(
            script,
            str(tmp_path / ".alkera"),
            "race",
            str(go_file),
            str(ready_dir / str(i)),
            str(bounced_dir),
            str(winner_file),
            str(release_file),
        )
        for i in range(n)
    ]

    import time as _time

    def _await(cond, *, timeout: float, message: str) -> None:  # type: ignore[no-untyped-def]
        """Poll ``cond`` until true or fail with ``message``. Deadlines are
        generous so transient CPU starvation can't trip them — they only fire on
        a genuine hang. The happy path returns in milliseconds."""
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if cond():
                return
            _time.sleep(0.02)
        raise AssertionError(message)

    # Every child is poised on the barrier.
    _await(
        lambda: len(list(ready_dir.iterdir())) == n,
        timeout=60.0,
        message=f"only {len(list(ready_dir.iterdir()))}/{n} subprocesses became ready",
    )

    # Open the gate — all children race the lock simultaneously.
    go_file.touch()

    # A winner emerged + holds the lock.
    _await(
        lambda: winner_file.exists(),
        timeout=60.0,
        message="no subprocess reported winning the lock",
    )

    # ...and EVERY loser has provably attempted + bounced while the winner STILL
    # holds the lock. Replaces the old fixed sleep — correctness no longer depends
    # on how fast a loser gets scheduled.
    _await(
        lambda: len(list(bounced_dir.iterdir())) == n - 1,
        timeout=60.0,
        message=(
            f"only {len(list(bounced_dir.iterdir()))}/{n - 1} losers bounced "
            "while the winner held the lock"
        ),
    )

    # Safe to release: the lock was held for the entire window all losers attempted.
    release_file.touch()

    outputs: list[dict] = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        assert p.returncode == 0, f"subprocess crashed: {stderr}"
        for line in stdout.splitlines():
            if line.strip():
                outputs.append(json.loads(line))

    winners = [o for o in outputs if o["result"] == "won"]
    blocked = [o for o in outputs if o["result"] == "blocked"]
    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}: {outputs}"
    assert len(blocked) == n - 1, f"expected {n - 1} blocked, got {len(blocked)}: {outputs}"


def test_clean_exit_releases_lock_via_atexit(tmp_path: Path) -> None:
    """A subprocess that acquires the lock + exits without explicitly
    releasing must still leave the lock file gone — `atexit` handler
    fires it."""
    store = ProjectDirectory(tmp_path / ".alkera").chats()
    chat = store.create(session_id="atexit-test")
    chat.close()

    script = textwrap.dedent(
        """
        import sys
        from alkera_core.project.directory import ProjectDirectory

        alkera_path, session_id = sys.argv[1], sys.argv[2]
        store = ProjectDirectory(alkera_path).chats()
        chat = store.open(session_id)
        # Deliberately DON'T call chat.close() — let atexit handle it.
        print(f"acquired pid={chat._lock.payload['pid']}")
        # exit normally → atexit runs
        """
    )
    result = _run_python(script, str(tmp_path / ".alkera"), "atexit-test", timeout=10)
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    # Lock file should NOT exist post-exit.
    assert not (store.path / "atexit-test" / ".lock").exists(), (
        f"lock file leaked: {result.stdout!r} stderr={result.stderr!r}"
    )


def test_sigkill_leaves_stale_lock_which_next_acquirer_reclaims(tmp_path: Path) -> None:
    """SIGKILL skips Python's atexit + signal handlers entirely. The
    next acquirer must detect the stale lock (dead PID) and reclaim."""
    store = ProjectDirectory(tmp_path / ".alkera").chats()
    chat = store.create(session_id="killed")
    chat.close()

    # Hold the lock in a subprocess that prints its PID then sleeps.
    script = textwrap.dedent(
        """
        import sys, time
        from alkera_core.project.directory import ProjectDirectory

        alkera_path, session_id = sys.argv[1], sys.argv[2]
        store = ProjectDirectory(alkera_path).chats()
        chat = store.open(session_id)
        # Tell the parent we've acquired + then block forever.
        print(chat._lock.payload['pid'], flush=True)
        time.sleep(120)
        """
    )
    p = _spawn_python(script, str(tmp_path / ".alkera"), "killed")
    try:
        # Wait for the subprocess to acquire + print its PID.
        assert p.stdout is not None
        # `readline` blocks until newline + flush from subprocess.
        line = p.stdout.readline()
        # The holder reports its own pid: on Windows the venv python.exe is a
        # launcher whose real interpreter is a grandchild, so p.pid can differ
        # — the lock payload carries the pid that must die to go stale.
        child_pid = int(line.strip())

        # Verify lock file exists with that PID.
        lock_path = store.path / "killed" / ".lock"
        assert lock_path.exists()
        payload = json.loads(lock_path.read_bytes())
        assert payload["pid"] == child_pid

        # Hard-kill the HOLDER — atexit + signal handlers DON'T run.
        kill_process(child_pid)
        # Reap: on POSIX the killed child is a zombie (still "alive" to a PID
        # probe) until waited on; on Windows the launcher exits with its child.
        p.wait(timeout=30)
        deadline = time.monotonic() + 10
        while process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(child_pid)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)

    # Lock file SHOULD still be there (no cleanup ran).
    assert lock_path.exists(), "expected SIGKILL to leave stale lock"
    # Now the parent process tries to acquire — should reclaim cleanly.
    chat2 = store.open("killed")
    try:
        # Our lock should have our PID, not the dead child's.
        assert chat2._lock.payload is not None
        assert chat2._lock.payload["pid"] == os.getpid()
    finally:
        chat2.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no cross-process SIGTERM with handlers on Windows (TerminateProcess "
    "skips them); hard death is covered by the stale-lock reclaim test",
)
def test_sigterm_handler_releases_lock(tmp_path: Path) -> None:
    """SIGTERM does run Python's signal handler stack. Our installed
    handler should release the lock before chaining through."""
    store = ProjectDirectory(tmp_path / ".alkera").chats()
    chat = store.create(session_id="sigterm-test")
    chat.close()

    script = textwrap.dedent(
        """
        import sys, time
        from alkera_core.project.directory import ProjectDirectory

        alkera_path, session_id = sys.argv[1], sys.argv[2]
        store = ProjectDirectory(alkera_path).chats()
        chat = store.open(session_id)
        print('acquired', flush=True)
        time.sleep(120)
        """
    )
    p = _spawn_python(script, str(tmp_path / ".alkera"), "sigterm-test")
    try:
        assert p.stdout is not None
        # Wait for "acquired" line.
        line = p.stdout.readline().strip()
        assert line == "acquired"

        lock_path = store.path / "sigterm-test" / ".lock"
        assert lock_path.exists()

        # SIGTERM — signal handler should release.
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)

    # Lock file should be gone — signal handler released it.
    assert not lock_path.exists(), (
        f"expected SIGTERM handler to release; lock still exists at {lock_path}"
    )


def test_serial_handoff(tmp_path: Path) -> None:
    """Process A acquires → exits cleanly → process B acquires. The
    canonical happy-path serial flow."""
    store = ProjectDirectory(tmp_path / ".alkera").chats()
    chat = store.create(session_id="handoff")
    chat.close()

    script = textwrap.dedent(
        """
        import sys
        from alkera_core.project.directory import ProjectDirectory

        alkera_path, session_id, label = sys.argv[1], sys.argv[2], sys.argv[3]
        store = ProjectDirectory(alkera_path).chats()
        chat = store.open(session_id)
        try:
            print(f"{label}:held")
        finally:
            chat.close()
        print(f"{label}:released")
        """
    )
    r1 = _run_python(script, str(tmp_path / ".alkera"), "handoff", "A", timeout=10)
    r2 = _run_python(script, str(tmp_path / ".alkera"), "handoff", "B", timeout=10)
    assert r1.returncode == 0 and "A:held" in r1.stdout and "A:released" in r1.stdout
    assert r2.returncode == 0 and "B:held" in r2.stdout and "B:released" in r2.stdout


# Sanity: silence pyright/mypy on the unused import.
_ = (LockHeldError, ChatNotFoundError)
