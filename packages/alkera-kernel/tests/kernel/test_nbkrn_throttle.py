"""The replace throttle, driven by an injected clock (no real waiting)."""

from __future__ import annotations

import itertools

import pytest
from _alkera_kernel.throttle import ReplaceThrottle


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_nbkrn_first_value_goes_out_at_once(clock: Clock) -> None:
    t: ReplaceThrottle[str] = ReplaceThrottle(0.1, clock)
    assert t.offer("a") == ("a", None)
    assert not t.has_pending


def test_nbkrn_values_inside_the_interval_wait_and_the_last_wins(clock: Clock) -> None:
    t: ReplaceThrottle[str] = ReplaceThrottle(0.1, clock)
    t.offer("a")
    clock.now += 0.01
    value, delay = t.offer("b")
    assert value is None and delay == pytest.approx(0.09)
    clock.now += 0.04
    assert t.offer("c") == (None, pytest.approx(0.05))
    clock.now += 0.04  # 0.09 after the first send: not yet
    assert t.due() == (None, pytest.approx(0.01))
    clock.now += 0.01
    assert t.due() == ("c", None)
    assert t.due() == (None, None)


def test_nbkrn_an_early_wake_never_sends_twice_in_an_interval(clock: Clock) -> None:
    """A timer armed for an older value that fires after an immediate send
    finds nothing it may send yet."""
    t: ReplaceThrottle[int] = ReplaceThrottle(0.1, clock)
    t.offer(1)
    clock.now += 0.05
    t.offer(2)  # waits; a wake is due at +0.10
    clock.now += 0.06  # +0.11: a new value goes out at once and replaces 2
    assert t.offer(3) == (3, None)
    clock.now += 0.001  # the stale wake fires now
    assert t.due() == (None, None)
    clock.now += 0.01
    assert t.offer(4) == (None, pytest.approx(0.089))
    assert t.due()[0] is None


def test_nbkrn_at_most_one_send_per_interval_over_a_long_burst(clock: Clock) -> None:
    t: ReplaceThrottle[int] = ReplaceThrottle(0.1, clock)
    sent: list[tuple[float, int]] = []
    wake: float | None = None
    for i in range(1000):  # one value every 5 ms for 5 s
        clock.now = 1000.0 + i * 0.005
        if wake is not None and clock.now >= wake:
            value, delay = t.due()
            if value is not None:
                sent.append((clock.now, value))
            wake = clock.now + delay if delay is not None else None
        value, delay = t.offer(i)
        if value is not None:
            sent.append((clock.now, value))
        elif wake is None and delay is not None:
            wake = clock.now + delay
    last = t.flush()
    assert last is not None
    times = [s for s, _ in sent]
    assert all(b - a >= 0.1 - 1e-9 for a, b in itertools.pairwise(times))
    assert 45 <= len(sent) <= 51
    assert last == 999


def test_nbkrn_flush_sends_the_waiting_value_at_cell_end(clock: Clock) -> None:
    t: ReplaceThrottle[str] = ReplaceThrottle(0.1, clock)
    t.offer("a")
    clock.now += 0.01
    t.offer("b")
    assert t.flush() == "b"
    assert t.flush() is None
