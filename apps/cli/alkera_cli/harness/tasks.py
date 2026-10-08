"""Ending a background task without losing the caller's own cancellation.

Every ``stop()`` in the harness and the cloud mirror ends a task the same way:
cancel it, then wait for it to finish so nothing is torn down under it.

``await task`` raises ``CancelledError`` in two unrelated cases: the task ended
by being cancelled (expected, swallowed) and the caller was cancelled while
waiting (a shutdown deadline, a test's ``asyncio.timeout``). A
``contextlib.suppress`` around the await cannot tell them apart, and swallowing
the second leaves the caller running past a cancellation it never saw.
``asyncio.wait`` never raises the awaited task's outcome, and a cancellation of
the caller propagates out of it like any other await.

One ``cancel()`` is also not proof the task was told. A cancellation that lands
while the task is not suspended on a cancellable future is only recorded, and
whatever the task does next may swallow it (an async generator catching
``CancelledError`` during cleanup does). So the wait is a loop: ask, wait a
while, ask again, until the task ends or the caller's patience does.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: How long one wait lasts before the task is asked to stop again. Long enough that a task
#: unwinding through its own cleanup finishes on the first ask, short enough that a task
#: which swallowed that ask is re-told promptly. A task that ends sooner is not made to wait:
#: this is the upper bound on one slice, not a delay.
RECANCEL_INTERVAL_S = 1.0


async def stop_task(task: asyncio.Task[Any], *, grace: float | None = None) -> bool:
    """Cancel ``task`` and wait for it to end. Returns whether it did.

    The task's own outcome — its cancellation, or an exception it ended with —
    is consumed here and never raised. The caller's own cancellation is not:
    it propagates, and the task is left cancelled but still running.

    With ``grace``, the wait ends after that many seconds and the task is
    abandoned — still cancelled, but no longer waited on — and ``False`` says
    so. Without one the wait is as long as the task takes to notice.
    """
    if task.done():
        _consume(task)
        return True
    loop = asyncio.get_running_loop()
    deadline = None if grace is None else loop.time() + grace
    while True:
        task.cancel()
        slice_seconds = RECANCEL_INTERVAL_S
        if deadline is not None:
            slice_seconds = min(slice_seconds, max(deadline - loop.time(), 0.0))
        done, _pending = await asyncio.wait({task}, timeout=slice_seconds)
        if task in done:
            _consume(task)
            return True
        if deadline is not None and loop.time() >= deadline:
            return False


def _consume(task: asyncio.Task[Any]) -> None:
    """Mark the task's exception, if any, as retrieved, so ending a task that
    failed on the way out is not reported at collection as one nobody heard."""
    if not task.cancelled():
        task.exception()


__all__ = ["RECANCEL_INTERVAL_S", "stop_task"]
