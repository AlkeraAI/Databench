"""Cross-process auth.yml change watcher.

Multiple VS Code windows on the same machine each spawn their own
daemon; they share ``~/.alkera/auth.yml``. When the user logs in via
window A, window B's daemon needs to find out — same for logout. We use
mtime polling instead of OS-level inotify/FSEvents to stay portable and
dependency-free; auth.yml changes are infrequent so a 2-second cadence
is cheap.

Single-fire vs continuous:
- The watcher loops until ``stop()`` is called or the asyncio task is
  cancelled.
- On every change, ``on_change()`` is invoked. The daemon's auth module
  uses that to re-read auth.yml, re-validate against the cloud, and
  emit ``auth.changed`` to the editor extension.

Async-friendly:
- ``start_async()`` spawns an asyncio task. Stat is offloaded to the
  default executor so we don't block the event loop. (Each stat is a
  single syscall; the offload is mostly defensive.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class AuthFileWatcher:
    """Poll ``auth.yml`` for changes. Async-driven.

    Compares ``(mtime, size)`` rather than mtime alone — covers the edge
    case where a rewrite preserves mtime to the second on systems with
    second-granularity timestamps.

    Treats "file missing" as a valid state. mtime/size are recorded as
    ``None`` when absent; transitions to/from None fire ``on_change``.
    """

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], Awaitable[None] | None],
        *,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._poll_interval = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_signature: tuple[float, int] | None = None
        self._initialized = False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_async(self) -> asyncio.Task[None]:
        """Begin polling in a background task. Returns the task so the
        caller can await it during shutdown."""
        if self.is_running:
            return self._task  # type: ignore[return-value]
        self._task = asyncio.create_task(self._run(), name="alkera-auth-watcher")
        return self._task

    async def stop(self) -> None:
        """Stop polling. Idempotent."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Best-effort shutdown; the polling task is fire-and-forget,
            # and we already cancelled it. Surface the failure via the
            # logger so it's not silently swallowed.
            from alkera_cli.daemon.logging_setup import get_logger

            get_logger("alkera.auth_watcher").exception("auth.watcher.stop.failed")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        # Seed the baseline so we don't fire on the very first tick.
        self._last_signature = await loop.run_in_executor(None, self._signature)
        self._initialized = True
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                current = await loop.run_in_executor(None, self._signature)
                if current != self._last_signature:
                    self._last_signature = current
                    result = self._on_change()
                    if asyncio.iscoroutine(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception:
                # Log via the structlog config the daemon already set up.
                # Importing at call time avoids a circular dep.
                from alkera_cli.daemon.logging_setup import get_logger

                get_logger("alkera.auth_watcher").exception("auth.watcher.tick.failed")

    def _signature(self) -> tuple[float, int] | None:
        try:
            st = self._path.stat()
            return (st.st_mtime, st.st_size)
        except FileNotFoundError:
            return None


__all__ = ["AuthFileWatcher"]
