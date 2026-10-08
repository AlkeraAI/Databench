"""Daemon-specific paths under `~/.alkera/`.

The daemon writes:
- Transient per-process state to ``~/.alkera/daemon/<pid>/`` (deleted on graceful shutdown)
- Log files to ``~/.alkera/logs/daemon.log`` (shared across daemons; rotating)

All paths are derived from the CLI's `ALKERA_HOME` so tests can isolate
them via the env-var override that ``alkera_cli.host.paths.ALKERA_HOME`` already
honors.
"""

from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

from alkera_cli.host import paths as cli_paths


def daemon_root() -> Path:
    """`~/.alkera/daemon/` — parent for every daemon's per-PID dir."""
    p = cli_paths.ALKERA_HOME / "daemon"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def daemon_working_dir(*, pid: int | None = None) -> Path:
    """Per-process scratch dir. Defaults to the current PID's slot.

    Auto-registers an ``atexit`` cleanup that wipes the dir on graceful
    shutdown. Crashes (SIGKILL, etc.) leave it behind; that's deliberate
    — leftover dirs are useful for post-mortem.
    """
    pid = pid or os.getpid()
    path = daemon_root() / str(pid)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _cleanup() -> None:
        # Best effort — don't raise from atexit if the dir is already gone.
        shutil.rmtree(path, ignore_errors=True)

    atexit.register(_cleanup)
    return path


def daemon_log_dir() -> Path:
    """`~/.alkera/logs/` — shared log directory across daemons."""
    p = cli_paths.ALKERA_HOME / "logs"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def daemon_log_file() -> Path:
    """The rotating daemon log file. Multiple daemons share it; their lines
    are distinguished by the PID prefix the logger emits.
    """
    return daemon_log_dir() / "daemon.log"


__all__ = [
    "daemon_log_dir",
    "daemon_log_file",
    "daemon_root",
    "daemon_working_dir",
]
