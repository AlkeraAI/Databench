"""The injectable clock and id source the whole Files library takes.

A fault injector that does not inject is a bug, and so is a fake clock that
does not move: these tests drive the seams across the boundaries the real
subsystems care about (a day roll, a monotonic deadline) instead of asserting
that a getter returns what a setter was handed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import RFC_4122, UUID

import pytest
from alkera_core.files.clock import (
    Clock,
    FakeClock,
    IdSource,
    SeededIdSource,
    SystemClock,
    SystemIdSource,
)

# Computed from the documented algorithm (uuid4-shaped ids drawn from
# `random.Random(1234).getrandbits(128)`) rather than by re-running the class
# under test, so a change of algorithm fails here instead of re-blessing itself.
SEED_1234_FIRST_THREE = (
    UUID("1de9ea66-70d3-4a1f-8735-df5ef7697fb9"),
    UUID("f149f542-e935-4870-9734-6b4501eaf614"),
    UUID("08f0ebd4-950c-4dd9-8e97-b5bdf073eed1"),
)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        pytest.param(
            timedelta(hours=23, minutes=59, seconds=59),
            datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC),
            id="one-second-before-the-day-roll",
        ),
        pytest.param(
            timedelta(days=1),
            datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
            id="exactly-on-the-day-roll",
        ),
        pytest.param(
            timedelta(days=1, microseconds=1),
            datetime(2026, 1, 2, 0, 0, 0, 1, tzinfo=UTC),
            id="one-microsecond-after-the-day-roll",
        ),
    ],
)
def test_advance_lands_on_each_side_of_the_day_boundary(
    clock: FakeClock, delta: timedelta, expected: datetime
) -> None:
    clock.advance(delta)

    assert clock.now() == expected
    assert clock.now().date() == expected.date()


def test_advance_crosses_the_day_boundary_in_steps(clock: FakeClock) -> None:
    """Two hops that each stay inside a day still roll the date together."""
    clock.advance(timedelta(hours=20))
    assert clock.now().day == 1

    clock.advance(timedelta(hours=5))

    assert clock.now() == datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC)


def test_advance_moves_wall_clock_and_monotonic_by_the_same_amount(
    clock: FakeClock,
) -> None:
    before = clock.monotonic()

    clock.advance(timedelta(seconds=90, milliseconds=500))

    assert clock.monotonic() - before == pytest.approx(90.5)
    assert clock.now() == datetime(2026, 1, 1, 0, 1, 30, 500_000, tzinfo=UTC)


def test_advance_refuses_to_run_backwards(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))

    assert clock.now() == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert clock.monotonic() == 0.0


def test_move_to_a_later_instant_moves_monotonic_forward(clock: FakeClock) -> None:
    clock.move_to(datetime(2026, 1, 3, 6, 0, 0, tzinfo=UTC))

    assert clock.now() == datetime(2026, 1, 3, 6, 0, 0, tzinfo=UTC)
    assert clock.monotonic() == pytest.approx(2 * 86400 + 6 * 3600)


def test_move_to_an_earlier_instant_never_rewinds_monotonic(clock: FakeClock) -> None:
    """The negative twin: a wall clock can step back (NTP), a deadline cannot."""
    clock.advance(timedelta(hours=3))
    monotonic_at_the_top = clock.monotonic()

    clock.move_to(datetime(2025, 12, 31, 12, 0, 0, tzinfo=UTC))

    assert clock.now() == datetime(2025, 12, 31, 12, 0, 0, tzinfo=UTC)
    assert clock.monotonic() == monotonic_at_the_top


def test_move_to_normalizes_a_non_utc_instant(clock: FakeClock) -> None:
    plus_two = timezone(timedelta(hours=2))

    clock.move_to(datetime(2026, 1, 1, 2, 0, 0, tzinfo=plus_two))

    assert clock.now() == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_a_naive_instant_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="aware"):
        FakeClock(now=datetime(2026, 1, 1))


def test_a_naive_instant_is_refused_by_move_to(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="aware"):
        clock.move_to(datetime(2026, 1, 2))

    assert clock.now() == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_fake_clock_starts_at_the_monotonic_offset_it_was_given() -> None:
    clock = FakeClock(now=datetime(2026, 6, 1, tzinfo=UTC), monotonic=1_000.0)

    assert clock.monotonic() == 1_000.0
    clock.advance(timedelta(seconds=1))
    assert clock.monotonic() == 1_001.0


def test_system_clock_now_is_aware_utc_and_monotonic_does_not_rewind() -> None:
    clock = SystemClock()

    first = clock.now()
    second = clock.now()

    assert first.tzinfo is not None
    assert first.utcoffset() == timedelta(0)
    assert second >= first
    earlier_tick = clock.monotonic()
    later_tick = clock.monotonic()
    assert later_tick >= earlier_tick


def test_both_clocks_satisfy_the_protocol() -> None:
    fake: Clock = FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))
    system: Clock = SystemClock()

    assert isinstance(fake.monotonic(), float)
    assert isinstance(system.monotonic(), float)


def test_seeded_ids_match_the_documented_sequence(ids: SeededIdSource) -> None:
    drawn = tuple(ids.uuid() for _ in range(3))

    assert drawn == SEED_1234_FIRST_THREE


def test_two_sources_with_the_same_seed_agree_step_for_step() -> None:
    left = SeededIdSource(7)
    right = SeededIdSource(7)

    assert [left.uuid() for _ in range(20)] == [right.uuid() for _ in range(20)]


def test_a_different_seed_diverges_immediately() -> None:
    """The negative twin of determinism: a seed is not ignored."""
    assert SeededIdSource(7).uuid() != SeededIdSource(8).uuid()


def test_a_seeded_source_never_repeats_within_a_run(ids: SeededIdSource) -> None:
    drawn = [ids.uuid() for _ in range(500)]

    assert len(set(drawn)) == 500


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(SeededIdSource(99), id="seeded"),
        pytest.param(SystemIdSource(), id="system"),
    ],
)
def test_every_id_source_emits_uuid4_shaped_ids(source: IdSource) -> None:
    drawn = source.uuid()

    assert drawn.version == 4
    assert drawn.variant == RFC_4122


def test_the_system_source_does_not_repeat() -> None:
    source = SystemIdSource()

    assert len({source.uuid() for _ in range(100)}) == 100
