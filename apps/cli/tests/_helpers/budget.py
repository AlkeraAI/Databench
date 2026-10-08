"""The seconds one test may spend waiting on real time, and a named step within them.

A test that drives a whole box — a service with four loops, a chat, a loader,
a file watcher — waits on things that happen on their own schedule, and every
one of those waits has to be bounded BY NAME. The suite's own per-test limit
is ninety seconds, and on Windows it ends the xdist worker: what the run then
reports is a dead worker with no test and no traceback. A step that carries a
generous minute of its own cannot fail by name either — two slow ones and the
suite's limit gets there first.

So a test holds one budget for everything it waits on, and each step is a
phase against it: the first to run out is named, and pytest is always left the
room to report it. A stop or a teardown gets a budget of its own, so running
out there cannot replace the failure that sent the test to it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class Budget:
    """The seconds one test may spend waiting, shared by every step of it."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._deadline = asyncio.get_running_loop().time() + seconds

    @property
    def seconds(self) -> float:
        return self._seconds

    @property
    def deadline(self) -> float:
        """When the budget ends, on the running loop's clock."""
        return self._deadline

    @property
    def left(self) -> float:
        return self._deadline - asyncio.get_running_loop().time()

    def ran_out(self, what: str) -> AssertionError:
        return AssertionError(f"{what} never finished; the test's {self._seconds:.0f}s ran out")


@contextlib.asynccontextmanager
async def phase(budget: Budget, what: str) -> AsyncIterator[None]:
    """Bound one step against the budget and NAME it when it never finishes.

    Everything in a test that can wait — a plugin discovery, a registration
    against a backend that is not there, a service's start and stop, a fixture's
    teardown — sits inside one of these, so a wedged run fails saying which step
    never returned instead of being killed at the suite's limit with a stack
    that is only the event loop.
    """
    try:
        async with asyncio.timeout(budget.left):
            yield
    except TimeoutError:
        raise budget.ran_out(what) from None


__all__ = ["Budget", "phase"]
