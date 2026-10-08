"""The CLI's one backoff owner: the growing wait, the reconnect backoff that a
successful connect never resets (it decays only after a sustained healthy
period), the bounded retry, and the Retry-After reader."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alkera_cli.host.backoff import (
    ReconnectBackoff,
    doubled,
    doubling_wait,
    exponential_delay,
    parse_retry_after,
    retry_after_of,
    retry_until,
)


class _Clock:
    def __init__(self, at: float = 0.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _fixed(value: float) -> ReconnectBackoff:
    return ReconnectBackoff(rng=lambda: value, clock=_Clock())


def test_delays_double_from_the_base_and_stop_at_the_cap() -> None:
    backoff = _fixed(0.5)  # jitter factor exactly 1.0
    delays = [backoff.next_delay() for _ in range(7)]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


@pytest.mark.parametrize(
    ("rng", "expected"),
    [
        pytest.param(0.0, 1.0, id="lower-bound-half"),
        pytest.param(0.5, 2.0, id="middle-exact"),
        pytest.param(0.999, 2.998, id="upper-bound-under-1.5x"),
    ],
)
def test_jitter_scales_the_delay_between_half_and_one_and_a_half(
    rng: float, expected: float
) -> None:
    assert _fixed(rng).next_delay() == pytest.approx(expected, rel=1e-6)


def test_a_successful_connect_never_resets_the_delay() -> None:
    """The rule that keeps a flapping fleet from arriving as one wave."""
    clock = _Clock()
    backoff = ReconnectBackoff(rng=lambda: 0.5, clock=clock)
    assert backoff.next_delay() == 2.0
    assert backoff.next_delay() == 4.0
    backoff.connected()
    assert backoff.attempt == 2
    # Dropped again right away: the next wait is the NEXT step, not the floor.
    assert backoff.next_delay() == 8.0


def test_the_delay_decays_one_step_per_healthy_period() -> None:
    clock = _Clock()
    backoff = ReconnectBackoff(rng=lambda: 0.5, clock=clock, healthy_period=300.0)
    for _ in range(3):
        backoff.next_delay()
    assert backoff.attempt == 3
    backoff.connected()
    clock.at = 299.0
    assert backoff.decay() == 3  # not yet a full period
    clock.at = 300.0
    assert backoff.decay() == 2
    clock.at = 601.0
    assert backoff.decay() == 1  # two periods elapsed in total, one already credited
    clock.at = 10_000.0
    assert backoff.decay() == 0  # never below zero
    assert backoff.next_delay() == 2.0


def test_decay_before_any_connect_changes_nothing() -> None:
    backoff = _fixed(0.5)
    backoff.next_delay()
    assert backoff.decay() == 1


def test_a_drop_after_connect_restarts_the_healthy_clock() -> None:
    clock = _Clock()
    backoff = ReconnectBackoff(rng=lambda: 0.5, clock=clock, healthy_period=100.0)
    backoff.next_delay()
    backoff.next_delay()
    backoff.connected()
    clock.at = 90.0
    backoff.next_delay()  # dropped before a full period: nothing was credited
    assert backoff.attempt == 3
    clock.at = 95.0
    backoff.connected()
    clock.at = 190.0
    assert backoff.decay() == 3  # only 95 s into the NEW connection
    clock.at = 195.0
    assert backoff.decay() == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"base": 0.0}, id="zero-base"),
        pytest.param({"base": 10.0, "cap": 5.0}, id="cap-below-base"),
        pytest.param({"healthy_period": 0.0}, id="zero-healthy-period"),
    ],
)
def test_invalid_parameters_are_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ReconnectBackoff(**kwargs)


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        pytest.param(0, 0.0, id="nothing-refused-no-wait"),
        pytest.param(1, 30.0, id="first-refusal-twice-the-base"),
        pytest.param(2, 60.0, id="doubles"),
        pytest.param(4, 240.0, id="still-under-the-cap"),
        pytest.param(5, 300.0, id="capped"),
        pytest.param(40, 300.0, id="stays-capped"),
    ],
)
def test_doubling_wait(failures: int, expected: float) -> None:
    assert doubling_wait(failures, base=15.0, cap=300.0) == expected


# --- the growing wait ---------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "first", "cap", "expected"),
    [
        pytest.param(0, 1.0, 60.0, 1.0, id="first-retry-waits-the-first-wait"),
        pytest.param(1, 1.0, 60.0, 2.0, id="doubles"),
        pytest.param(5, 1.0, 60.0, 32.0, id="still-under-the-cap"),
        pytest.param(6, 1.0, 60.0, 60.0, id="capped"),
        pytest.param(5, 0.25, 4.0, 4.0, id="a-fractional-first-wait-reaches-its-cap"),
        pytest.param(10_000, 1.0, 60.0, 60.0, id="a-huge-attempt-is-the-cap-not-an-overflow"),
        pytest.param(3, 0.0, 60.0, 0.0, id="a-zero-first-wait-stays-zero"),
    ],
)
def test_exponential_delay(attempt: int, first: float, cap: float, expected: float) -> None:
    assert exponential_delay(attempt, first=first, cap=cap) == expected


def test_exponential_delay_refuses_a_negative_attempt() -> None:
    with pytest.raises(ValueError):
        exponential_delay(-1, first=1.0, cap=2.0)


@pytest.mark.parametrize(
    ("previous", "expected"),
    [
        pytest.param(0.0, 2.0, id="a-streak-starts-at-the-floor"),
        pytest.param(0.5, 2.0, id="a-wait-below-the-floor-rises-to-it"),
        pytest.param(2.0, 4.0, id="doubles"),
        pytest.param(400.0, 600.0, id="capped"),
        pytest.param(600.0, 600.0, id="stays-capped"),
    ],
)
def test_doubled(previous: float, expected: float) -> None:
    assert doubled(previous, floor=2.0, cap=600.0) == expected


# --- the bounded retry ----------------------------------------------------------


class _Wall:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _BusyError(Exception):
    pass


def _refusing(times: int) -> tuple[list[int], Callable[[], str]]:
    calls: list[int] = []

    def attempt() -> str:
        calls.append(1)
        if len(calls) <= times:
            raise _BusyError
        return "taken"

    return calls, attempt


def test_retry_until_returns_what_the_attempt_returns_once_it_stops_refusing() -> None:
    wall = _Wall()
    calls, attempt = _refusing(3)
    taken = retry_until(
        attempt,
        retry_on=_BusyError,
        timeout=10.0,
        first=0.5,
        cap=1.0,
        sleep=wall.sleep,
        clock=wall.clock,
    )
    assert taken == "taken"
    assert wall.slept == [0.5, 1.0, 1.0]
    assert len(calls) == 4


def test_retry_until_raises_the_refusal_that_arrives_at_the_deadline() -> None:
    wall = _Wall()
    _, attempt = _refusing(1_000)
    with pytest.raises(_BusyError):
        retry_until(
            attempt,
            retry_on=_BusyError,
            timeout=3.0,
            first=1.0,
            cap=1.0,
            sleep=wall.sleep,
            clock=wall.clock,
        )
    assert wall.slept == [1.0, 1.0, 1.0], "gave up on the try at the three-second mark"


def test_retry_until_lets_any_other_failure_through_at_once() -> None:
    wall = _Wall()

    def attempt() -> None:
        raise KeyError("not a refusal")

    with pytest.raises(KeyError):
        retry_until(
            attempt,
            retry_on=_BusyError,
            timeout=3.0,
            first=1.0,
            cap=1.0,
            sleep=wall.sleep,
            clock=wall.clock,
        )
    assert wall.slept == []


# --- Retry-After ------------------------------------------------------------------

_NOW = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("3", 3.0, id="whole-seconds"),
        pytest.param(" 3.5 ", 3.5, id="fractional-seconds-padded"),
        pytest.param("0", 0.0, id="zero-is-a-wait-of-zero"),
        pytest.param("inf", float("inf"), id="infinity-is-left-for-the-caller-to-cap"),
        pytest.param("-1", None, id="negative"),
        pytest.param("nan", None, id="nan"),
        pytest.param("soon", None, id="garbage"),
        pytest.param("", None, id="empty"),
        pytest.param(None, None, id="absent"),
        pytest.param("Wed, 21 Oct 2026 07:28:30 GMT", None, id="a-date-without-a-wall-clock"),
    ],
)
def test_parse_retry_after_reads_delta_seconds_only_by_default(
    value: str | None, expected: float | None
) -> None:
    assert parse_retry_after(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("Wed, 21 Oct 2026 07:28:30 GMT", 30.0, id="a-date-ahead"),
        pytest.param("Wed, 21 Oct 2026 07:27:00 GMT", 0.0, id="a-date-already-past-asks-no-wait"),
        pytest.param("Wed, 21 Oct 2026 07:28:00 GMT", 0.0, id="a-date-of-now"),
        pytest.param("12", 12.0, id="seconds-still-win-over-the-clock"),
        pytest.param("21 Oct 2026", None, id="a-date-with-no-time"),
        pytest.param("not a date", None, id="garbage"),
    ],
)
def test_parse_retry_after_reads_a_date_against_the_wall_clock_it_is_given(
    value: str, expected: float | None
) -> None:
    assert parse_retry_after(value, wall_clock=lambda: _NOW) == expected


def test_a_dated_retry_after_shrinks_as_the_wall_clock_moves() -> None:
    value = "Wed, 21 Oct 2026 07:29:00 GMT"
    assert parse_retry_after(value, wall_clock=lambda: _NOW) == 60.0
    later = _NOW + timedelta(seconds=45)
    assert parse_retry_after(value, wall_clock=lambda: later) == 15.0
    assert parse_retry_after(value, wall_clock=lambda: later + timedelta(minutes=5)) == 0.0


def test_retry_after_of_reads_the_refused_responses_header() -> None:
    request = httpx.Request("GET", "http://drive")

    def refused(headers: dict[str, str]) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            "slow down", request=request, response=httpx.Response(429, headers=headers)
        )

    assert retry_after_of(refused({"Retry-After": "7"})) == 7.0
    assert retry_after_of(refused({})) is None
    assert retry_after_of(ValueError("carries no response")) is None
    dated = refused({"Retry-After": "Wed, 21 Oct 2026 07:28:30 GMT"})
    assert retry_after_of(dated) is None, "a date needs a wall clock"
    assert retry_after_of(dated, wall_clock=lambda: _NOW) == 30.0
