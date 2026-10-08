"""The one rule for an org worker that keeps failing: what makes it failing
on the beat, what makes it a crash loop for the alarm, and what ends each."""

from __future__ import annotations

import pytest
from alkera_cli.supervisor.crash_loop import CrashLoop, Failure

ORG_A = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
ORG_B = "be0a3393-082f-4acf-918d-6eac00904c9b"


def _quick(rule: CrashLoop, org: str, times: int, *, start: float = 0.0) -> list[Failure]:
    """``times`` failures a second apart, each a second after its start."""
    return [rule.failed(org, now=start + n, ran=1.0) for n in range(times)]


def test_the_shipped_thresholds() -> None:
    """Three in a row is failing; the sixth within fifteen minutes is a loop;
    a minute up is settled."""
    rule = CrashLoop()
    seen = _quick(rule, ORG_A, 6)
    assert [f.began_failing for f in seen] == [False, False, True, False, False, False]
    assert [f.looping for f in seen] == [None, None, None, None, None, 6]
    assert rule.window == 900.0
    assert (rule.settled(59.9), rule.settled(60.0)) == (False, True)


def test_the_third_failure_in_a_row_makes_the_org_failing_and_says_so_once() -> None:
    rule = CrashLoop(failures=3)
    first, second = _quick(rule, ORG_A, 2)
    assert (first.streak, second.streak) == (1, 2)
    assert not first.began_failing and not second.began_failing
    assert rule.failing == frozenset()
    third = rule.failed(ORG_A, now=2, ran=1.0)
    assert (third.streak, third.began_failing) == (3, True)
    assert rule.failing == frozenset({ORG_A})
    fourth = rule.failed(ORG_A, now=3, ran=1.0)
    assert (fourth.streak, fourth.began_failing) == (4, False)
    assert rule.failing == frozenset({ORG_A})


def test_a_start_that_did_not_happen_counts_like_a_quick_exit() -> None:
    rule = CrashLoop(failures=2)
    rule.failed(ORG_A, now=0, ran=None)
    assert rule.failed(ORG_A, now=1, ran=None).began_failing
    assert rule.failing == frozenset({ORG_A})


@pytest.mark.parametrize(
    ("ran", "streak"),
    [
        pytest.param(59.9, 3, id="just-under-settled-continues-the-streak"),
        pytest.param(60.0, 1, id="settled-starts-a-new-streak-at-one"),
        pytest.param(3600.0, 1, id="long-run"),
    ],
)
def test_a_failure_after_a_settled_run_starts_the_streak_over(ran: float, streak: int) -> None:
    rule = CrashLoop(failures=3, settled_seconds=60.0)
    _quick(rule, ORG_A, 2)
    failure = rule.failed(ORG_A, now=100, ran=ran)
    assert failure.streak == streak
    assert rule.failing == (frozenset({ORG_A}) if streak == 3 else frozenset())


def test_a_failure_after_a_settled_run_is_the_first_of_the_next_streak() -> None:
    rule = CrashLoop(failures=3, settled_seconds=60.0)
    rule.failed(ORG_A, now=0, ran=120.0)
    rule.failed(ORG_A, now=1, ran=1.0)
    assert rule.failing == frozenset()
    assert rule.failed(ORG_A, now=2, ran=1.0).began_failing


@pytest.mark.parametrize(
    ("ran", "still_failing"),
    [
        pytest.param(59.9, True, id="not-yet-settled"),
        pytest.param(60.0, False, id="settled"),
    ],
)
def test_a_worker_that_settles_ends_the_streak(ran: float, still_failing: bool) -> None:
    rule = CrashLoop(failures=3, settled_seconds=60.0)
    _quick(rule, ORG_A, 3)
    rule.up(ORG_A, ran=ran)
    assert (ORG_A in rule.failing) is still_failing
    # After a settled run the next streak needs all three again.
    again = rule.failed(ORG_A, now=10, ran=1.0)
    assert again.streak == (4 if still_failing else 1)


def test_a_worker_asked_to_stop_ends_the_streak_and_leaves_the_window() -> None:
    rule = CrashLoop(failures=3, exits=5, window=900.0)
    _quick(rule, ORG_A, 5)
    rule.stopped(ORG_A)
    assert rule.failing == frozenset()
    sixth = rule.failed(ORG_A, now=5, ran=1.0)
    assert (sixth.streak, sixth.looping) == (1, 6)


@pytest.mark.parametrize(
    ("times", "looping"),
    [
        pytest.param(5, None, id="at-the-limit"),
        pytest.param(6, 6, id="one-past"),
        pytest.param(7, 7, id="and-on-every-failure-after"),
    ],
)
def test_more_failures_in_the_window_than_the_limit_is_a_loop(
    times: int, looping: int | None
) -> None:
    rule = CrashLoop(exits=5, window=900.0)
    assert _quick(rule, ORG_A, times)[-1].looping == looping


@pytest.mark.parametrize(
    ("at", "looping"),
    [
        pytest.param(899.9, 6, id="the-first-is-still-in-the-window"),
        pytest.param(900.0, None, id="the-first-is-exactly-a-window-old"),
        pytest.param(5000.0, None, id="long-after"),
    ],
)
def test_a_failure_a_window_old_no_longer_counts(at: float, looping: int | None) -> None:
    rule = CrashLoop(exits=5, window=900.0)
    _quick(rule, ORG_A, 5)  # at 0, 1, 2, 3 and 4 seconds
    assert rule.failed(ORG_A, now=at, ran=1.0).looping == looping


def test_a_worker_that_dies_after_every_settled_run_loops_without_ever_failing() -> None:
    rule = CrashLoop(failures=3, settled_seconds=60.0, exits=5, window=900.0)
    seen = [rule.failed(ORG_A, now=n * 120.0, ran=118.0) for n in range(6)]
    assert [f.streak for f in seen] == [1] * 6
    assert seen[-1].looping == 6
    assert rule.failing == frozenset()


def test_settling_does_not_empty_the_window() -> None:
    rule = CrashLoop(exits=5, window=900.0)
    _quick(rule, ORG_A, 5)
    rule.up(ORG_A, ran=600.0)
    assert rule.failed(ORG_A, now=700, ran=600.0).looping == 6


def test_an_org_that_left_the_box_is_forgotten_whole() -> None:
    rule = CrashLoop(failures=3, exits=5)
    _quick(rule, ORG_A, 6)
    rule.forget(ORG_A)
    assert rule.failing == frozenset()
    again = rule.failed(ORG_A, now=10, ran=1.0)
    assert (again.streak, again.began_failing, again.looping) == (1, False, None)


def test_each_org_has_its_own_record() -> None:
    rule = CrashLoop(failures=3, exits=5)
    _quick(rule, ORG_A, 6)
    other = rule.failed(ORG_B, now=6, ran=1.0)
    assert (other.streak, other.looping) == (1, None)
    assert rule.failing == frozenset({ORG_A})
    rule.forget(ORG_B)
    assert rule.failing == frozenset({ORG_A})
