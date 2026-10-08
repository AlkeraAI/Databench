"""Retrying the two Postgres failures that are a *scheduling* verdict, not a bug.

`READ COMMITTED` plus explicit row locks means Files never asks
Postgres to serialize a transaction for it — but two of Postgres' own errors
still reach the service boundary and mean nothing more than "you two collided,
run it again":

``40001`` ``serialization_failure``
    the snapshot the statement read is no longer usable.
``40P01`` ``deadlock_detected``
    the deadlock detector picked this transaction as the victim.

Both are safe to replay because the transaction that raised them left nothing
behind: Postgres rolled it back whole. Nothing else is. A unique violation, a
check violation, a lost `if_match`, a store fault — replaying those either
repeats an effect or hides a bug, so this retries **only** the two SQLSTATEs
above and re-raises everything else on the first attempt, with no delay.

Exactly-once is not this module's job. A replay re-runs the operation, so the
operation must be the idempotent one: wrap a call that already goes through
:func:`alkera_core.files.idempotency.idempotent`, whose committed key row makes
the second attempt a replay of the first answer rather than a second effect.
The clock and the sleeper are injected so a test drives a real retry ladder
without a real wait.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, TypeVar

from alkera_core.db.errors import sqlstate_of
from alkera_core.files.clock import Clock

T = TypeVar("T")

#: The only two failures a replay is allowed to answer.
RETRYABLE_SQLSTATES: Final = frozenset({"40001", "40P01"})


def is_retryable(exc: BaseException) -> bool:
    """Whether `exc` is one of the two replayable Postgres verdicts."""
    return sqlstate_of(exc) in RETRYABLE_SQLSTATES


@dataclass(frozen=True, slots=True)
class RetryBudget:
    """How many replays are allowed, and how long they may take.

    `attempts` counts the *total* runs including the first, so `attempts=1` is
    "never retry". `window`, when set, is a ceiling on the wall time spent
    across the ladder measured on the clock's monotonic reading: once the next
    delay would cross it the last error is raised rather than slept on, so a
    caller behind an HTTP timeout cannot be held past it.
    """

    attempts: int = 3
    base_delay: float = 0.05
    max_delay: float = 1.0
    window: timedelta | None = None

    def __post_init__(self) -> None:
        if self.attempts < 1:
            msg = f"a budget must allow at least one attempt (got {self.attempts})"
            raise ValueError(msg)
        if self.base_delay < 0 or self.max_delay < 0:
            msg = "retry delays may not be negative"
            raise ValueError(msg)

    def delay_before(self, attempt: int) -> float:
        """The pause before `attempt` (2 is the first retry): capped exponential."""
        grown = self.base_delay * float(2 ** (attempt - 2))
        return min(grown, self.max_delay)


async def with_db_retries(
    op: Callable[[], Awaitable[T]],
    *,
    budget: RetryBudget,
    clock: Clock,
    sleep: Callable[[float], Awaitable[None]],
) -> T:
    """Run `op`, replaying it only on `40001` / `40P01`.

    Returns `op`'s value. Raises the *last* failure once the budget or the
    window is spent, and any non-retryable failure at once — the caller can
    tell the two apart because the exception it sees is the driver's own.
    """
    started = clock.monotonic()
    attempt = 1
    while True:
        try:
            return await op()
        except BaseException as exc:
            if not is_retryable(exc) or attempt >= budget.attempts:
                raise
            delay = budget.delay_before(attempt + 1)
            if budget.window is not None:
                spent = clock.monotonic() - started
                if spent + delay > budget.window.total_seconds():
                    raise
            attempt += 1
        await sleep(delay)


__all__ = [
    "RETRYABLE_SQLSTATES",
    "RetryBudget",
    "T",
    "is_retryable",
    "sqlstate_of",
    "with_db_retries",
]
