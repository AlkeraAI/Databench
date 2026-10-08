"""Driving a coroutine from synchronous code, whichever thread asks."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")


def run_coro_blocking(coro: Coroutine[Any, Any, _T]) -> _T:
    """Drive a coroutine to completion from SYNC code, loop-safe.

    The connectors resolve OAuth credentials at the I/O boundary in sync code
    (a token refresh, a relay lease). That code usually runs on a worker thread
    where ``asyncio.run`` is fine — but the connection PROBE reaches the same
    resolvers on the daemon's event-loop thread, where ``asyncio.run`` raises
    "cannot be called from a running event loop". Detect that case and drive the
    coroutine in a dedicated thread with its own loop instead; blocking here is
    no worse than the blocking driver connect that immediately follows.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


__all__ = ["run_coro_blocking"]
