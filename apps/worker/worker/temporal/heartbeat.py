"""A liveness heartbeat for activities whose core runs unchanged.

The money sweeps declare a heartbeat timeout so a worker that dies mid-sweep is
noticed within minutes rather than at the thirty-minute deadline. Their cores
are the coroutines the queue-era tasks ran, and they know nothing about Temporal;
rather than thread a progress callback through every one of them, the activity
wrapper runs the core under a ticker that heartbeats on a fixed cadence. This
is a liveness signal, not a progress signal: the start-to-close timeout stays
the backstop for a core that hangs inside one database call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from temporalio import activity

DEFAULT_INTERVAL_S = 30.0
"""A quarter of the shortest heartbeat timeout in the policy table, so a
dropped tick never costs the attempt."""


@asynccontextmanager
async def heartbeating(every_s: float = DEFAULT_INTERVAL_S) -> AsyncIterator[None]:
    """Heartbeat every ``every_s`` seconds for the duration of the block when
    running inside an activity; a no-op when the same core is driven directly
    by a test or a CLI, where there is no activity context to heartbeat to."""
    if every_s <= 0:
        raise ValueError(f"heartbeat interval must be positive, got {every_s!r}")
    if not activity.in_activity():
        yield
        return

    async def tick() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(every_s)

    ticker = asyncio.create_task(tick())
    try:
        yield
    finally:
        ticker.cancel()
        # Wait for the ticker without adopting its outcome. Awaiting the task
        # directly would raise ITS CancelledError here, and absorbing that would
        # also absorb a cancellation aimed at the activity that lands at this
        # very await — the activity would run on as if never cancelled. `wait`
        # never raises the ticker's; the only CancelledError that can leave this
        # block is the activity's own, and it propagates.
        await asyncio.wait({ticker})
        if not ticker.cancelled():
            ticker.result()  # a ticker that died on its own is not a silent stop
