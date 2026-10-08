"""Ending the tasks a stopping box was running, within a bound.

A stop never ``await``s a task outright: one whose cancellation was absorbed
by the library it was inside never ends, and a stop waiting on it is a box
that keeps beating and holding leases while serving nothing. The cancel is
asked again through a grace, and a task that has still not ended is named,
with where it is parked, and left behind.
"""

from __future__ import annotations

import asyncio
import logging
import sysconfig
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def parked_at(task: asyncio.Task[Any]) -> str:
    """Where a suspended task is parked, for the log line that names a task the
    stop left behind: the chain of this codebase's own frames (outermost first)
    and the innermost library await under them. ``Task.get_stack`` stops at
    the outermost coroutine; this follows the ``cr_await`` chain down."""
    stdlib = sysconfig.get_path("stdlib")
    own: list[str] = []
    innermost = "an unknown await"
    coro: Any = task.get_coro()
    for _ in range(64):
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
        if frame is None:
            break
        filename = frame.f_code.co_filename
        innermost = f"{Path(filename).name}:{frame.f_lineno} in {frame.f_code.co_name}"
        if "site-packages" not in filename and not filename.startswith(stdlib):
            own.append(innermost)
        coro = getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
    if not own:
        return innermost
    if own[-1] == innermost:
        return " > ".join(own)
    return f"{' > '.join(own)} (awaiting {innermost})"


async def settle(
    tasks: Sequence[asyncio.Task[Any]],
    grace: float,
    *,
    every: float,
    log: logging.Logger,
) -> list[str]:
    """Cancel ``tasks`` and wait at most ``grace`` for them to end, asking
    again every ``every`` seconds. Returns the names of the tasks still running
    after that, each logged on ``log`` with the line it is parked on."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    pending = {task for task in tasks if not task.done()}
    while pending:
        for task in pending:
            task.cancel()
        left = deadline - loop.time()
        if left <= 0:
            break
        _done, pending = await asyncio.wait(pending, timeout=min(left, every))
    abandoned: list[str] = []
    for task in tasks:
        if task.done():
            if not task.cancelled() and task.exception() is not None:
                log.warning(
                    "%s ended with an error before the stop: %r", task.get_name(), task.exception()
                )
            continue
        log.warning(
            "%s did not end within %.1fs of being cancelled; it is parked at %s and left behind",
            task.get_name(),
            grace,
            parked_at(task),
        )
        abandoned.append(task.get_name())
    return abandoned


__all__ = ["parked_at", "settle"]
