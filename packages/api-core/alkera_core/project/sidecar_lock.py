"""Thread-and-process-safe guards for ``.alkera/context/`` sidecars.

A ``.alkera`` ``FileLock`` is a *cross-process* primitive whose instance state is
NOT thread-safe, and the daemon hits the store from its ``asyncio.to_thread``
pool, so a sidecar (``context.yml`` / ``watermarks.json``) is touched by several
threads. Each cross-process ``FileLock`` is therefore paired with a per-path
in-process ``threading.Lock``. LanceDB's own commit lock covers the vector
table; this is only for the plain-file sidecars.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alkera_core.project.locking import FileLock, retrying_lock

_thread_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _registry_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


@contextmanager
def sidecar_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Guard a sidecar file with BOTH the in-process ``threading.Lock`` and the
    cross-process ``FileLock`` (held only for the brief write)."""
    tlock = _thread_lock_for(path)
    flock = FileLock(Path(str(path) + ".lock"))
    with tlock, retrying_lock(flock, timeout_seconds=timeout_seconds):
        yield


__all__ = ["sidecar_lock"]
