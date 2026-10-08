"""Whether the running task has been asked to stop, whatever exception the
request turned into on the way.

``task.cancel()`` raises ``CancelledError`` at the task's next await. When that
await is inside a library that cleans up and raises its own error instead
(httpx answering a cancel that met a read timeout with the ``ReadTimeout``),
the ``CancelledError`` is gone but the request is still recorded on the task.
A loop that catches the library's error and goes round again then never
stops, and whoever awaits the task waits for good.
"""

from __future__ import annotations

import asyncio


def cancel_was_requested() -> bool:
    """True when the current task has a cancel request it has not honoured."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def raise_if_cancel_requested() -> None:
    """Honour a cancel request another exception swallowed on the way."""
    if cancel_was_requested():
        raise asyncio.CancelledError


__all__ = ["cancel_was_requested", "raise_if_cancel_requested"]
