"""Parent-death watchdog for the daemon's seed worker subprocesses.

The daemon spawns ``alkera lineage seed`` / ``alkera context seed`` workers to keep the
heavy parse/embed out of its own interpreter. If the daemon dies (the IDE is closed, the
process is killed) while a worker is running, that worker must NOT keep going: on macOS
there is no OS primitive (``PR_SET_PDEATHSIG`` / a Windows Job Object) to tie a child's
lifetime to its parent, so a re-parented worker would otherwise keep parsing — and, worse,
race the *next* daemon's fresh seed of the same store (two writers).

The daemon sets ``ALKERA_SEED_PARENT_PID`` in the worker's environment; the worker calls
:func:`watch_parent` once at startup, which polls the parent's liveness on a daemon thread
and hard-exits the moment it's gone. The lineage SQLite + KB LanceDB stores are crash-safe
(WAL / atomic rename), so a hard exit loses nothing — the next daemon's seed resumes from
the durable parse marks / pending vectors. A worker run directly from the CLI (no env var)
is a no-op: it has no daemon parent to watch.
"""

from __future__ import annotations

import os
import threading
import time

from alkera_core.process import process_alive

#: Set by the daemon on each spawned seed worker; absent for a direct CLI invocation.
SEED_PARENT_PID_ENV = "ALKERA_SEED_PARENT_PID"

#: How often the watchdog checks that the parent is still alive. A few seconds is plenty —
#: a seed worker is a background batch job, not latency-sensitive — and keeps the poll cheap.
_POLL_INTERVAL_SECONDS = 2.0


def watch_parent(*, poll_interval: float = _POLL_INTERVAL_SECONDS) -> bool:
    """Start a background watchdog that exits this process if its spawning daemon dies.

    Reads the parent PID from ``ALKERA_SEED_PARENT_PID``. Returns ``True`` if a watchdog
    was started (the env var named a live parent), ``False`` otherwise (direct CLI run, or
    the parent is already gone — in which case we exit immediately). Never raises."""
    raw = os.environ.get(SEED_PARENT_PID_ENV)
    if not raw:
        return False  # not daemon-spawned — nothing to watch
    try:
        parent_pid = int(raw)
    except ValueError:
        return False
    if parent_pid <= 0:
        return False
    if not process_alive(parent_pid):
        # The daemon already died between spawn and our startup — don't even begin the work.
        os._exit(0)

    threading.Thread(
        target=_watch_loop,
        args=(parent_pid, poll_interval),
        name="seed-parent-watchdog",
        daemon=True,
    ).start()
    return True


def _watch_loop(parent_pid: int, poll_interval: float) -> None:
    """Poll the parent until it's gone, then hard-exit. Hard exit (no cleanup) is correct:
    the stores are crash-safe and the next daemon's seed resumes from the durable marks — a
    graceful shutdown would only do more (now-orphaned) work."""
    while process_alive(parent_pid):
        time.sleep(poll_interval)
    os._exit(0)


__all__ = ["SEED_PARENT_PID_ENV", "watch_parent"]
