"""Interleave checkpoints — the seam that makes concurrency testable.

Files calls `await cp.reach("after_put_before_commit")` at every point between
two effects whose order matters. In production the checkpoints object is
`NoopCheckpoints` and the call costs a coroutine step. In a test it is
`PausingCheckpoints`, which parks the coroutine at a named point until the test
releases it (forcing an exact interleaving between two real sessions) or raises
`CheckpointKilled` there (the crash harness turns that into a SIGKILL).

One seam, two uses: a test chooses `pause` or `kill` per name. Everything here
is asyncio-only — no threads, no sleeps to let the other side win.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

DEFAULT_TIMEOUT = 5.0
"""How long an armed checkpoint waits before it calls the test a deadlock."""


# The `Error` suffix N818 wants would read as a production failure; this is a
# test-harness assertion.
class CheckpointTimeout(AssertionError):  # noqa: N818
    """A checkpoint the test armed was never released (or never reached)."""


class CheckpointKilled(BaseException):
    """Raised where a killed checkpoint is reached.

    A `BaseException` on purpose: the scenario under test is supposed to die
    here the way a SIGKILL would, so a broad `except Exception` in the code
    being crash-tested cannot swallow the kill and hide the bug.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"checkpoint {name!r} was killed by the test")
        self.name = name


class Checkpoints(Protocol):
    """The contract Files code calls; production passes `NoopCheckpoints`."""

    async def reach(self, name: str) -> None:
        """Arrive at a named point from async code."""
        ...

    def reach_sync(self, name: str) -> None:
        """Arrive at a named point from synchronous code (an `AtomicWriter` step)."""
        ...

    def as_hook(self) -> Callable[[str], None]:
        """A plain callable for code that cannot take the object itself."""
        ...


class NoopCheckpoints:
    """The production implementation: every checkpoint is free."""

    async def reach(self, name: str) -> None:
        return None

    def reach_sync(self, name: str) -> None:
        return None

    def as_hook(self) -> Callable[[str], None]:
        return self.reach_sync


class PausingCheckpoints:
    """The test implementation: arm a name to pause or kill the code that reaches it.

    Lives beside the production default rather than in the test tree because
    both the crash harness and the concurrency fixtures drive it, and because
    the checkpoint names are part of the library's contract.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._release: dict[str, asyncio.Event] = {}
        self._arrival: dict[str, asyncio.Event] = {}
        self._killed: set[str] = set()
        self._reached: list[str] = []

    @property
    def reached(self) -> tuple[str, ...]:
        """Every checkpoint name the code has arrived at, in order."""
        return tuple(self._reached)

    def pause(self, name: str) -> None:
        """Arm `name` so the next arrival blocks until `release(name)`."""
        self._release[name] = asyncio.Event()
        self._arrival.setdefault(name, asyncio.Event())

    def release(self, name: str) -> None:
        """Let the coroutine parked at `name` (or the next one to arrive) continue."""
        event = self._release.pop(name, None)
        if event is None:
            msg = f"{name!r} is not paused"
            raise KeyError(msg)
        event.set()

    def kill(self, name: str) -> None:
        """Arm `name` so arriving there raises `CheckpointKilled`."""
        self._killed.add(name)

    # ASYNC109: the per-call `timeout` is the seam's documented signature — a test
    # waits a different amount for a checkpoint than the object's default deadline.
    async def wait_paused(
        self,
        name: str,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None:
        """Block until a coroutine is parked at `name`."""
        arrival = self._arrival.setdefault(name, asyncio.Event())
        try:
            await asyncio.wait_for(arrival.wait(), self._timeout if timeout is None else timeout)
        except TimeoutError:
            msg = (
                f"checkpoint {name!r} was never reached (armed by the test, never hit by the code)"
            )
            raise CheckpointTimeout(msg) from None

    async def reach(self, name: str) -> None:
        self._record(name)
        event = self._release.get(name)
        if event is None:
            return
        self._arrival.setdefault(name, asyncio.Event()).set()
        try:
            await asyncio.wait_for(event.wait(), self._timeout)
        except TimeoutError:
            msg = f"checkpoint {name!r} was never released (armed by the test, reached by the code)"
            raise CheckpointTimeout(msg) from None
        finally:
            self._release.pop(name, None)
            self._arrival.pop(name, None)

    def reach_sync(self, name: str) -> None:
        self._record(name)
        if name in self._release:
            msg = (
                f"checkpoint {name!r} is paused but was reached from synchronous "
                f"code; a sync checkpoint can only be killed, not paused"
            )
            raise RuntimeError(msg)

    def as_hook(self) -> Callable[[str], None]:
        return self.reach_sync

    def _record(self, name: str) -> None:
        self._reached.append(name)
        if name in self._killed:
            raise CheckpointKilled(name)
