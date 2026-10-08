"""The readiness latch: a shared-dependency blip must not read as a dead task,
and a task that has never been ready must never be graced.

Driven across the window's edge with freezegun on the latch's default clock, so
what is pinned is the real ``time.monotonic`` read, not an injected stand-in.
"""

from __future__ import annotations

import pytest
from alkera_core.readiness import ReadinessLatch
from freezegun import freeze_time

GRACE = 900.0


def _latch(grace: float = GRACE) -> ReadinessLatch:
    return ReadinessLatch(grace_seconds=lambda: grace)


def test_a_process_that_has_never_been_ready_is_refused_at_once() -> None:
    """A new revision that cannot reach its database must fail its first checks,
    or the deployment circuit breaker never sees a broken image."""
    latch = _latch()
    assert latch.still_ready() is False
    assert latch.has_been_ready is False


@pytest.mark.parametrize(
    ("after", "graced"),
    [
        pytest.param(0.0, True, id="the-same-instant"),
        pytest.param(GRACE - 0.001, True, id="just-inside-the-window"),
        pytest.param(GRACE, False, id="exactly-the-window"),
        pytest.param(GRACE + 60.0, False, id="well-past-it"),
    ],
)
def test_a_task_that_was_ready_is_graced_only_inside_the_window(after: float, graced: bool) -> None:
    with freeze_time("2026-09-30T00:20:00Z") as frozen:
        latch = _latch()
        latch.ready()
        frozen.tick(after)
        assert latch.still_ready() is graced


def test_every_ready_probe_restarts_the_window() -> None:
    """The window is measured from the LAST ready probe, so a flapping database
    that keeps coming back never runs a healthy task out of grace."""
    with freeze_time("2026-09-30T00:20:00Z") as frozen:
        latch = _latch()
        latch.ready()
        frozen.tick(GRACE - 1)
        latch.ready()
        frozen.tick(GRACE - 1)
        assert latch.still_ready() is True
        frozen.tick(1)
        assert latch.still_ready() is False


def test_a_grace_of_zero_is_the_strict_probe() -> None:
    with freeze_time("2026-09-30T00:20:00Z"):
        latch = _latch(0.0)
        latch.ready()
        assert latch.still_ready() is False


def test_the_window_is_read_on_every_call() -> None:
    """A deployment that retunes the setting takes effect without a restart."""
    grace = {"seconds": GRACE}
    with freeze_time("2026-09-30T00:20:00Z") as frozen:
        latch = ReadinessLatch(grace_seconds=lambda: grace["seconds"])
        latch.ready()
        frozen.tick(60)
        assert latch.still_ready() is True
        grace["seconds"] = 30.0
        assert latch.still_ready() is False


def test_reset_forgets_every_ready_probe() -> None:
    latch = _latch()
    latch.ready()
    latch.reset()
    assert latch.still_ready() is False
    assert latch.has_been_ready is False
