"""Store retries: full-jitter backoff spent out of a token-bucket budget.

The SDK's own retries are off (``max_attempts=1``) so that retrying is a single
decision made here, where the budget is. A budget matters more than the per-call
policy: when a store browns out, every in-flight request retries at once and the
retries are what keeps it down. The bucket caps the *fleet-wide* retry rate for a
driver instance; once it is empty a call fails fast with the store's own error
instead of queueing more work onto a store that is already failing.

Both the clock and the jitter source are injected, so a test asserts the exact
sleep schedule rather than sleeping through it.
"""

from __future__ import annotations

import random as _random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from alkera_core.files.clock import Clock

T = TypeVar("T")

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[], float]

__all__ = ["Jitter", "RetryBudget", "RetryPolicy", "Sleep", "with_retries"]


class RetryBudget:
    """A token bucket: one token per retry, refilled at a fixed rate.

    ``tokens`` is both the starting balance and the ceiling, so a long quiet
    period buys a burst of exactly one bucket's worth of retries and no more.
    """

    __slots__ = ("_capacity", "_clock", "_last", "_refill_per_second", "_tokens")

    def __init__(self, tokens: float, refill_per_second: float, *, clock: Clock) -> None:
        if tokens < 0:
            msg = f"a retry budget cannot start negative (got {tokens!r})"
            raise ValueError(msg)
        if refill_per_second < 0:
            msg = f"a retry budget cannot refill backwards (got {refill_per_second!r})"
            raise ValueError(msg)
        self._tokens = float(tokens)
        self._capacity = float(tokens)
        self._refill_per_second = float(refill_per_second)
        self._clock = clock
        self._last = clock.monotonic()

    @property
    def tokens(self) -> float:
        """The balance as of now, refill included."""
        self._refill()
        return self._tokens

    def try_consume(self, amount: float = 1.0) -> bool:
        """Spend ``amount`` tokens, or report that the budget is empty."""
        self._refill()
        if self._tokens < amount:
            return False
        self._tokens -= amount
        return True

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
        self._last = now


@dataclass(frozen=True)
class RetryPolicy:
    """Full-jitter exponential backoff, capped.

    Full jitter — ``uniform(0, min(cap, base * 2**n))`` — rather than the
    exponential delay itself, because equal delays re-synchronise every client
    that failed together into a second thundering herd.
    """

    max_attempts: int = 5
    base: float = 0.2
    cap: float = 5.0
    jitter: Jitter = field(default=_random.random, repr=False, compare=False)

    def delay(self, attempt: int) -> float:
        """The sleep before attempt ``attempt + 1``; ``attempt`` is 1-based."""
        if attempt < 1:
            msg = f"attempt is 1-based (got {attempt!r})"
            raise ValueError(msg)
        ceiling = min(self.cap, self.base * (2.0 ** (attempt - 1)))
        return self.jitter() * ceiling


async def with_retries(
    op: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    budget: RetryBudget,
    clock: Clock,
    sleep: Sleep,
    retryable: Callable[[BaseException], bool],
) -> T:
    """Run ``op``, retrying only failures ``retryable`` accepts.

    The raised error carries ``attempts`` and ``retry_elapsed`` so a caller can
    tell "the store said no once" from "we spent the whole budget on it".
    """
    started = clock.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await op()
        except Exception as exc:
            _stamp(exc, attempt, clock.monotonic() - started)
            if not retryable(exc):
                raise
            if attempt >= policy.max_attempts:
                raise
            if not budget.try_consume():
                raise
            floor = getattr(exc, "retry_after", None)
            delay = policy.delay(attempt)
            if isinstance(floor, int | float):
                delay = max(delay, float(floor))
            await sleep(delay)


def _stamp(exc: BaseException, attempts: int, elapsed: float) -> None:
    exc.attempts = attempts  # type: ignore[attr-defined]
    exc.retry_elapsed = elapsed  # type: ignore[attr-defined]
