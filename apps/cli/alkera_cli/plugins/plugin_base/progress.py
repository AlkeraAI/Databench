"""A tiny progress side-channel for long background seeds.

A seed runs in a child process the daemon spawned; it has no RPC channel back. So it
writes its progress to a small JSON sidecar (atomic, throttled) that the daemon merges
into ``scheduler.list`` — the editor polls that, so the Background Jobs panel shows a
live status line + bar. The sidecar is a SIDE channel: losing it never affects
correctness, and a missing/stale file just means "no detailed progress" (the job still
shows its running state). The daemon clears it when the job completes.

``total=None`` reports an INDETERMINATE bar (the work size isn't known yet); a
``current``/``total`` pair reports a determinate fraction.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alkera_core.atomic_io import write_json_atomic

#: The daemon sets this to the sidecar path when it spawns a seed worker; a direct CLI
#: run / a test leaves it unset, so progress is a no-op there.
PROGRESS_FILE_ENV = "ALKERA_PROGRESS_FILE"


class ProgressReporter:
    """Writes ``{status_text, current, total, updated_at}`` to a sidecar, THROTTLED so a
    per-relation update over thousands of relations doesn't thrash the disk. A ``None``
    path makes every method a no-op (the default outside the daemon)."""

    def __init__(
        self,
        path: Path | None,
        *,
        min_interval: float = 0.25,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._min_interval = min_interval
        self._clock = clock
        self._last = 0.0
        self._status = ""
        self._current: int | None = None
        self._total: int | None = None

    def update(
        self,
        *,
        status_text: str | None = None,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        if self._path is None:
            return
        if status_text is not None:
            self._status = status_text
        if current is not None:
            self._current = current
        if total is not None:
            self._total = total
        now = self._clock()
        if now - self._last < self._min_interval:
            return  # throttle — the consumer tolerates ≤ min_interval staleness
        self._last = now
        self._write(now)

    def _write(self, now: float) -> None:
        if self._path is None:
            return
        payload: dict[str, Any] = {"status_text": self._status, "updated_at": now}
        if self._current is not None:
            payload["current"] = self._current
        if self._total is not None:
            payload["total"] = self._total
        with contextlib.suppress(OSError):  # progress is best-effort — never break a seed
            write_json_atomic(self._path, payload)

    def finish(self, *, complete: bool, message: str | None = None) -> None:
        """Write a TERMINAL record (``done=True`` + whether the run did everything) — the
        worker calls this when the seed returns, so the daemon can tell "did everything"
        from "more to do next run". Force-flushed (never throttled); a no-op when there's
        no sidecar. If the worker is killed before this, the daemon sees the absence of
        ``done`` and treats the run as interrupted/incomplete."""
        if self._path is None:
            return
        payload: dict[str, Any] = {
            "done": True,
            "complete": complete,
            "message": message,
            "status_text": message or self._status,
            "updated_at": self._clock(),
        }
        with contextlib.suppress(OSError):
            write_json_atomic(self._path, payload)

    def clear(self) -> None:
        if self._path is None:
            return
        with contextlib.suppress(FileNotFoundError, OSError):
            self._path.unlink()


#: A reporter that does nothing — used when no sidecar path is configured.
NULL_PROGRESS = ProgressReporter(None)


def reporter_from_env(*, min_interval: float = 0.25) -> ProgressReporter:
    """The reporter writing to ``$ALKERA_PROGRESS_FILE`` (the daemon sets it per seed
    worker), or a no-op reporter when it's unset (a direct CLI run / a test)."""
    path = os.environ.get(PROGRESS_FILE_ENV)
    return ProgressReporter(Path(path), min_interval=min_interval) if path else NULL_PROGRESS


def read_progress(path: Path) -> dict[str, Any] | None:
    """Read a progress sidecar, or None if it's absent / torn / mid-write."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = [
    "NULL_PROGRESS",
    "PROGRESS_FILE_ENV",
    "ProgressReporter",
    "read_progress",
    "reporter_from_env",
]
