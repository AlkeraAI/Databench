"""The retry budget: what gets retried, how long we wait, and when we stop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.store._retry import RetryBudget, RetryPolicy, with_retries
from alkera_core.files.store.errors import NotFound, Throttled, Unavailable
from alkera_core.files.store.s3_compatible import _retryable

EPOCH = datetime(2026, 3, 1, tzinfo=UTC)


class Sleeper:
    """Records what `with_retries` asked to wait and advances the clock by it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


class Schedule:
    """An operation that raises a scripted sequence, then returns a value."""

    def __init__(self, failures: list[BaseException], value: str = "ok") -> None:
        self._failures = list(failures)
        self._value = value
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return self._value


def jitter_of(*values: float) -> Callable[[], float]:
    """A deterministic jitter source; the last value repeats forever."""
    remaining = list(values)

    def draw() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return draw


def _run(
    failures: list[BaseException],
    *,
    tokens: float = 10.0,
    refill: float = 0.0,
) -> tuple[Schedule, Sleeper, FakeClock, RetryBudget]:
    clock = FakeClock(EPOCH)
    return (
        Schedule(failures),
        Sleeper(clock),
        clock,
        RetryBudget(tokens, refill, clock=clock),
    )


async def test_retries_a_throttle_and_returns_the_eventual_success() -> None:
    op, sleeper, clock, budget = _run([Throttled("slow down"), Throttled("slow down")])
    policy = RetryPolicy(base=0.2, cap=5.0, jitter=jitter_of(1.0))

    result = await with_retries(
        op,
        policy=policy,
        budget=budget,
        clock=clock,
        sleep=sleeper,
        retryable=_retryable,
    )

    assert result == "ok"
    assert op.calls == 3
    # Full jitter at its ceiling: base, then base*2.
    assert sleeper.delays == [0.2, 0.4]


async def test_backoff_is_the_jittered_fraction_of_the_capped_ceiling() -> None:
    op, sleeper, clock, budget = _run([Unavailable("500")] * 5)
    policy = RetryPolicy(max_attempts=5, base=2.0, cap=5.0, jitter=jitter_of(0.5, 0.25, 1.0, 0.1))

    with pytest.raises(Unavailable):
        await with_retries(
            op, policy=policy, budget=budget, clock=clock, sleep=sleeper, retryable=_retryable
        )

    # ceilings: 2, 4, min(5, 8)=5, min(5, 16)=5 — each scaled by its jitter draw.
    assert sleeper.delays == [1.0, 1.0, 5.0, 0.5]


async def test_retry_after_is_a_floor_the_jitter_cannot_undercut() -> None:
    op, sleeper, clock, budget = _run([Throttled("slow down", retry_after=1.5)])
    policy = RetryPolicy(base=0.2, jitter=jitter_of(0.0))

    await with_retries(
        op, policy=policy, budget=budget, clock=clock, sleep=sleeper, retryable=_retryable
    )

    assert sleeper.delays == [1.5]


async def test_a_longer_backoff_wins_over_a_shorter_retry_after() -> None:
    op, sleeper, clock, budget = _run([Throttled("slow down", retry_after=0.05)])
    policy = RetryPolicy(base=4.0, jitter=jitter_of(1.0))

    await with_retries(
        op, policy=policy, budget=budget, clock=clock, sleep=sleeper, retryable=_retryable
    )

    assert sleeper.delays == [4.0]


async def test_an_empty_budget_stops_retrying_before_max_attempts() -> None:
    op, sleeper, clock, budget = _run([Unavailable("500")] * 6, tokens=2.0)
    policy = RetryPolicy(max_attempts=5, jitter=jitter_of(0.0))

    with pytest.raises(Unavailable) as raised:
        await with_retries(
            op, policy=policy, budget=budget, clock=clock, sleep=sleeper, retryable=_retryable
        )

    assert op.calls == 3  # the first attempt plus the two tokens
    assert raised.value.attempts == 3  # type: ignore[attr-defined]
    assert budget.tokens == pytest.approx(0.0)


async def test_the_budget_refills_across_the_clock_and_buys_retries_again() -> None:
    clock = FakeClock(EPOCH)
    budget = RetryBudget(1.0, 0.5, clock=clock)

    assert budget.try_consume() is True
    assert budget.try_consume() is False
    clock.advance(timedelta(seconds=1))  # 0.5 tokens — still short
    assert budget.try_consume() is False
    clock.advance(timedelta(seconds=1))  # now one whole token
    assert budget.try_consume() is True


async def test_the_budget_never_refills_past_its_ceiling() -> None:
    clock = FakeClock(EPOCH)
    budget = RetryBudget(2.0, 10.0, clock=clock)

    clock.advance(timedelta(seconds=60))

    assert budget.tokens == pytest.approx(2.0)


async def test_max_attempts_caps_the_run_even_with_budget_to_spare() -> None:
    op, sleeper, clock, budget = _run([Unavailable("500")] * 10, tokens=100.0)
    policy = RetryPolicy(max_attempts=3, jitter=jitter_of(0.0))

    with pytest.raises(Unavailable):
        await with_retries(
            op, policy=policy, budget=budget, clock=clock, sleep=sleeper, retryable=_retryable
        )

    assert op.calls == 3
    assert len(sleeper.delays) == 2
    assert budget.tokens == pytest.approx(98.0)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        pytest.param(Unavailable("access denied"), 403, id="403-forbidden"),
        pytest.param(Unavailable("bad request"), 400, id="400-bad-request"),
        pytest.param(NotFound("no such key"), 404, id="404-not-found"),
        pytest.param(Unavailable("precondition"), 412, id="412-precondition"),
    ],
)
async def test_a_4xx_other_than_429_is_never_retried(error: Exception, status: int) -> None:
    error.status_code = status  # type: ignore[attr-defined]
    op, sleeper, clock, budget = _run([error] * 5)

    with pytest.raises(type(error)):
        await with_retries(
            op,
            policy=RetryPolicy(jitter=jitter_of(0.0)),
            budget=budget,
            clock=clock,
            sleep=sleeper,
            retryable=_retryable,
        )

    assert op.calls == 1
    assert sleeper.delays == []
    assert budget.tokens == pytest.approx(10.0)  # a refusal costs no budget


async def test_a_429_is_retried_even_though_it_is_a_4xx() -> None:
    throttle = Throttled("slow down")
    throttle.status_code = 429  # type: ignore[attr-defined]
    op, sleeper, clock, budget = _run([throttle])

    await with_retries(
        op,
        policy=RetryPolicy(jitter=jitter_of(0.0)),
        budget=budget,
        clock=clock,
        sleep=sleeper,
        retryable=_retryable,
    )

    assert op.calls == 2


async def test_a_connection_fault_is_retried() -> None:
    op, sleeper, clock, budget = _run([ConnectionError("reset by peer")])

    result = await with_retries(
        op,
        policy=RetryPolicy(jitter=jitter_of(0.0)),
        budget=budget,
        clock=clock,
        sleep=sleeper,
        retryable=_retryable,
    )

    assert result == "ok"
    assert op.calls == 2


async def test_a_budget_cannot_start_negative_or_refill_backwards() -> None:
    clock = FakeClock(EPOCH)
    with pytest.raises(ValueError, match="negative"):
        RetryBudget(-1.0, 1.0, clock=clock)
    with pytest.raises(ValueError, match="backwards"):
        RetryBudget(1.0, -1.0, clock=clock)


def test_the_policy_rejects_a_zero_based_attempt() -> None:
    with pytest.raises(ValueError, match="1-based"):
        RetryPolicy().delay(0)
