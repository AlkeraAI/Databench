"""Deterministic simulation scheduler for the Files concurrency suite.

Actors are plain coroutines that yield at every interesting point with
`await sim.step(label)` and take time with `await sim.sleep(seconds)`. The
scheduler — not asyncio — decides who runs next, drawing from
`random.Random(seed)` under a `Policy`, so a whole multi-actor run is one
integer. A failure carries the trace tail and the exact `pytest … --sim-seed=`
line that reproduces it.

The simulated clock only ever moves inside the scheduler (when every actor is
asleep), so a lease TTL, a trash window or a day roll is crossed on purpose and
never by wall-clock luck.
"""

from __future__ import annotations

import os
import random
from collections.abc import Awaitable, Coroutine, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from alkera_core.files.clock import FakeClock

SIM_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""Where every simulated clock starts, matching the suite's `EPOCH`."""

MAX_STEPS = 100_000
"""A run that never ends is a bug in an actor, not a reason to hang CI."""


# The spec names this fault `Partitioned` and actors read as `except Partitioned`;
# an `Error` suffix would read as a production failure rather than an injected one.
class Partitioned(Exception):  # noqa: N818
    """Raised inside an actor whose steps fall in a `PartitionPolicy` window."""


class TraceEntry(NamedTuple):
    """One scheduling decision: who ran, at which label, at what simulated time."""

    actor: str
    label: str
    sim_time: float


@dataclass
class _Step:
    label: str


@dataclass
class _Sleep:
    seconds: float


class _Yield:
    """The awaitable an actor suspends on; the scheduler receives the request."""

    __slots__ = ("request",)

    def __init__(self, request: _Step | _Sleep) -> None:
        self.request = request

    def __await__(self) -> Generator[_Step | _Sleep, None, None]:
        yield self.request


@dataclass
class Actor:
    """One simulated process: a coroutine plus where the scheduler left it."""

    name: str
    coro: Coroutine[Any, Any, None]
    pending: _Step | None = None
    wake_at: float | None = None
    steps: int = 0
    done: bool = False

    @property
    def pending_label(self) -> str | None:
        return None if self.pending is None else self.pending.label


class Policy:
    """How the scheduler picks the next actor, and what it does to it."""

    def choose(self, sim: Sim, runnable: Sequence[Actor], rng: random.Random) -> Actor | None:
        """Pick one runnable actor, or `None` to let simulated time move instead."""
        raise NotImplementedError

    def fault(self, sim: Sim, actor: Actor, step_index: int) -> BaseException | None:
        """An exception to raise *at* this step instead of running it."""
        return None


class RandomPolicy(Policy):
    """Uniformly random interleaving — the baseline explorer."""

    def choose(self, sim: Sim, runnable: Sequence[Actor], rng: random.Random) -> Actor | None:
        return rng.choice(list(runnable))


class PauseLongestPolicy(Policy):
    """Park the busiest actor for k rounds: a stop-the-world longer than a lease TTL.

    While it is parked the other actors keep running and sleeping, so simulated
    time marches past the TTL and the parked actor wakes into a world that has
    moved on — exactly the shape that reveals an unfenced writer.
    """

    def __init__(self, rounds: int = 25, *, after: int = 2) -> None:
        self.rounds = rounds
        self.after = after
        self._parked: str | None = None
        self._remaining = 0

    def choose(self, sim: Sim, runnable: Sequence[Actor], rng: random.Random) -> Actor | None:
        if self._parked is None and sim.total_steps >= self.after:
            live = [a for a in sim.actors if not a.done]
            if live:
                busiest = max(live, key=lambda a: (a.steps, a.name))
                self._parked = busiest.name
                self._remaining = self.rounds
        eligible = [a for a in runnable if a.name != self._parked]
        if self._parked is not None:
            self._remaining -= 1
            if self._remaining <= 0:
                self._parked = None
        if not eligible:
            return None
        return rng.choice(eligible)


class ReorderCommitsPolicy(Policy):
    """Hold back steps labelled `commit` while any other actor can run."""

    def __init__(self, delay: int = 8, *, label: str = "commit") -> None:
        self.delay = delay
        self.label = label
        self._deferred: dict[str, int] = {}

    def choose(self, sim: Sim, runnable: Sequence[Actor], rng: random.Random) -> Actor | None:
        commits = [a for a in runnable if a.pending_label == self.label]
        others = [a for a in runnable if a.pending_label != self.label]
        if commits and others:
            ready = False
            for actor in commits:
                count = self._deferred.get(actor.name, 0) + 1
                self._deferred[actor.name] = count
                ready = ready or count > self.delay
            if not ready:
                return rng.choice(others)
        return rng.choice(list(runnable))


class PartitionPolicy(Policy):
    """One actor's steps raise `Partitioned` inside `[from_step, to_step)`.

    Steps are counted per actor and zero-based, so a window names the same
    steps on every replay of the seed.
    """

    def __init__(
        self,
        actor: str,
        from_step: int,
        to_step: int,
        *,
        inner: Policy | None = None,
    ) -> None:
        if from_step < 0 or to_step < from_step:
            msg = f"partition window must be a forward range (got [{from_step}, {to_step}))"
            raise ValueError(msg)
        self.actor = actor
        self.from_step = from_step
        self.to_step = to_step
        self.inner: Policy = inner if inner is not None else RandomPolicy()

    def choose(self, sim: Sim, runnable: Sequence[Actor], rng: random.Random) -> Actor | None:
        return self.inner.choose(sim, runnable, rng)

    def fault(self, sim: Sim, actor: Actor, step_index: int) -> BaseException | None:
        if actor.name == self.actor and self.from_step <= step_index < self.to_step:
            return Partitioned(f"{actor.name} is partitioned at step {step_index}")
        return None


class Sim:
    """A seeded run of N actors over a simulated clock."""

    def __init__(
        self,
        seed: int,
        *,
        policy: Policy | None = None,
        start: datetime = SIM_EPOCH,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.seed = seed
        self.policy: Policy = policy if policy is not None else RandomPolicy()
        self.clock = FakeClock(now=start)
        self.trace: list[TraceEntry] = []
        self.actors: list[Actor] = []
        self.max_steps = max_steps
        self._rng = random.Random(seed)
        self._started = False

    @property
    def time(self) -> float:
        """Simulated seconds since the run began."""
        return self.clock.monotonic()

    @property
    def total_steps(self) -> int:
        return len(self.trace)

    def spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> Actor:
        """Register an actor coroutine; it does not run until `run()`."""
        if self._started:
            msg = "spawn every actor before run(); a mid-run spawn is not replayable"
            raise RuntimeError(msg)
        if any(a.name == name for a in self.actors):
            msg = f"actor names must be unique (got a second {name!r})"
            raise ValueError(msg)
        actor = Actor(name=name, coro=coro)
        self.actors.append(actor)
        return actor

    def step(self, label: str) -> Awaitable[None]:
        """Yield to the scheduler at a named point."""
        return _Yield(_Step(label))

    def sleep(self, seconds: float) -> Awaitable[None]:
        """Sleep in simulated time; the clock moves only when everyone is asleep."""
        if seconds < 0:
            msg = f"cannot sleep backwards (got {seconds!r})"
            raise ValueError(msg)
        return _Yield(_Sleep(seconds))

    def run(self) -> None:
        """Drive every actor to completion under the policy."""
        self._start()
        while True:
            if self.total_steps > self.max_steps:
                msg = f"sim exceeded {self.max_steps} steps\n{self.failure_report()}"
                raise AssertionError(msg)
            runnable = [a for a in self.actors if a.pending is not None]
            if not runnable:
                if self._advance_clock():
                    continue
                break
            chosen = self.policy.choose(self, runnable, self._rng)
            if chosen is None:
                if self._advance_clock():
                    continue
                chosen = self._rng.choice(runnable)
            self._resume(chosen)

    @contextmanager
    def report_on_failure(self) -> Iterator[None]:
        """Attach the trace tail and the replay command to anything raised inside."""
        try:
            yield
        except BaseException as exc:
            self.annotate(exc)
            raise

    def replay_command(self) -> str:
        """The exact line a developer runs to reproduce this run."""
        current = os.environ.get("PYTEST_CURRENT_TEST", "")
        test_id = current.split(" (")[0].strip() if current else "<file>::<test>"
        return f"pytest {test_id} --sim-seed={self.seed}"

    def failure_report(self, tail: int = 25) -> str:
        """Human-readable trace tail plus the replay command."""
        lines = [
            f"sim seed={self.seed} policy={type(self.policy).__name__} t={self.time:.3f}",
            f"trace tail (last {min(tail, len(self.trace))} of {len(self.trace)}):",
        ]
        lines += [f"  {e.actor} {e.label} t={e.sim_time:.3f}" for e in self.trace[-tail:]]
        lines.append(f"replay: {self.replay_command()}")
        return "\n".join(lines)

    def annotate(self, exc: BaseException) -> None:
        """Attach the failure report to `exc` exactly once."""
        note = self.failure_report()
        if note not in getattr(exc, "__notes__", []):
            exc.add_note(note)

    def _start(self) -> None:
        if self._started:
            msg = "a Sim runs once; build a new one to replay the seed"
            raise RuntimeError(msg)
        self._started = True
        for actor in self.actors:
            self._pump(actor)

    def _advance_clock(self) -> bool:
        wakes = [a.wake_at for a in self.actors if a.wake_at is not None]
        if not wakes:
            return False
        delta = min(wakes) - self.time
        if delta > 0:
            self.clock.advance(timedelta(seconds=delta))
        for actor in self.actors:
            if actor.wake_at is not None and actor.wake_at <= self.time:
                actor.wake_at = None
                actor.pending = _Step("wake")
        return True

    def _resume(self, actor: Actor) -> None:
        request = actor.pending
        if request is None:  # pragma: no cover - the scheduler only picks runnables
            msg = f"{actor.name} was resumed without a pending step"
            raise RuntimeError(msg)
        actor.pending = None
        step_index = actor.steps
        actor.steps += 1
        self.trace.append(TraceEntry(actor.name, request.label, self.time))
        fault = self.policy.fault(self, actor, step_index)
        try:
            self._pump(actor, throw=fault)
        except BaseException as exc:
            self.annotate(exc)
            raise

    def _pump(self, actor: Actor, *, throw: BaseException | None = None) -> None:
        try:
            nxt = actor.coro.throw(throw) if throw is not None else actor.coro.send(None)
        except StopIteration:
            actor.done = True
            return
        except BaseException:
            actor.done = True
            raise
        self._dispatch(actor, nxt)

    def _dispatch(self, actor: Actor, request: object) -> None:
        if isinstance(request, _Step):
            actor.pending = request
        elif isinstance(request, _Sleep):
            actor.wake_at = self.time + request.seconds
        else:
            msg = (
                f"{actor.name} awaited something the sim does not drive ({request!r}); "
                "actors may only await sim.step(), sim.sleep() and sim-aware helpers"
            )
            raise TypeError(msg)
