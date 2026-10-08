"""Cancel the asyncio tasks a timed-out test left running on a shared loop.

pytest-timeout's signal method raises inside whatever the main thread is doing
when the alarm fires. Under pytest-asyncio that is the event loop's selector
poll, so the exception unwinds ``run_until_complete`` and the test fails — but
the task running the test coroutine is neither finished nor cancelled. It stays
pending on the loop, and because the suite shares one loop for the whole
session, the next test's ``run_until_complete`` resumes it. Its ``finally``
blocks and ``async with`` exits never run; everything it still holds — a served
uvicorn stub, a service's heartbeat loop, an open socket — keeps running under
every later test on that worker. One such test left a mirror service beating
against its stub server every 50 ms for the remaining two hours of a run.

So the root ``conftest.py`` snapshots the loop's pending tasks before each
async test and, when pytest-timeout ends the test, cancels the tasks that
appeared since and drains them for a bounded time. A task that honours
cancellation runs its cleanup and is gone; one that does not is named, with
the line it is parked on, rather than waited on forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

#: The text pytest-timeout puts in the failure it raises (``pytest.fail``), and
#: the one thing that tells a timed-out test apart from a test that failed on
#: its own: only the former leaves its coroutine parked on the loop.
PYTEST_TIMEOUT_SIGNATURE = "from pytest-timeout"


@dataclass(frozen=True, slots=True)
class Reaped:
    """What cancelling the abandoned tasks achieved."""

    #: Names of the tasks that ended once cancelled.
    cancelled: tuple[str, ...]
    #: Tasks still pending when the drain window closed, each named with the
    #: source line it is parked on.
    survivors: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.cancelled or self.survivors)


def pending_tasks(loop: asyncio.AbstractEventLoop) -> frozenset[asyncio.Task[Any]]:
    """The tasks not yet done on ``loop`` — the snapshot to take before a test runs."""
    if loop.is_closed():
        return frozenset()
    return frozenset(task for task in asyncio.all_tasks(loop) if not task.done())


def is_pytest_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is the failure pytest-timeout raised to end a test.

    An ordinary failure, even one whose message mentions a timeout, is raised
    by the test coroutine itself and therefore finishes its task; only
    pytest-timeout's own failure abandons the task mid-await.
    """
    return isinstance(exc, pytest_failed_type()) and PYTEST_TIMEOUT_SIGNATURE in str(exc)


def pytest_failed_type() -> type[BaseException]:
    """The exception ``pytest.fail`` raises, resolved lazily so importing this
    module never imports pytest into production code."""
    import pytest

    return pytest.fail.Exception


def reap_abandoned_tasks(
    loop: asyncio.AbstractEventLoop,
    before: Collection[asyncio.Task[Any]],
    *,
    drain_seconds: float,
) -> Reaped:
    """Cancel every pending task on ``loop`` that is not in ``before`` and let
    the loop run, for at most ``drain_seconds``, so the cancellation lands and
    each task's cleanup executes.

    Tasks in ``before`` — a session fixture's listener, a module server — are
    never touched: they were alive before the test and are somebody else's to
    stop. Only tasks the test created, its own coroutine included, are reaped.

    Must be called with the loop NOT running (between tests, from sync code).
    """
    if loop.is_closed():
        return Reaped((), ())
    known = set(before)
    leftover = [task for task in asyncio.all_tasks(loop) if task not in known and not task.done()]
    if not leftover:
        return Reaped((), ())
    for task in leftover:
        task.cancel()

    async def drain() -> set[asyncio.Task[Any]]:
        _done, pending = await asyncio.wait(leftover, timeout=drain_seconds)
        return pending

    pending = loop.run_until_complete(drain())
    cancelled = tuple(sorted(task.get_name() for task in leftover if task not in pending))
    survivors = tuple(sorted(_describe(task) for task in pending))
    return Reaped(cancelled=cancelled, survivors=survivors)


def _describe(task: asyncio.Task[Any]) -> str:
    frames = task.get_stack(limit=1)
    if not frames:
        return task.get_name()
    frame = frames[-1]
    return f"{task.get_name()} parked at {frame.f_code.co_filename}:{frame.f_lineno}"


def format_report(reaped: Reaped, *, drain_seconds: float) -> str:
    """The lines a test report carries about what the timeout left behind."""
    lines = [f"cancelled: {name}" for name in reaped.cancelled]
    lines += [
        f"STILL PENDING after {drain_seconds:g}s (ignores cancellation): {name}"
        for name in reaped.survivors
    ]
    return "\n".join(lines)


__all__ = [
    "PYTEST_TIMEOUT_SIGNATURE",
    "Reaped",
    "format_report",
    "is_pytest_timeout",
    "pending_tasks",
    "pytest_failed_type",
    "reap_abandoned_tasks",
]
