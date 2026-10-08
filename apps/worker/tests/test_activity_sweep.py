"""The shared sweep shape: the run's clock, and the core under its advisory lock.

The lock is real Postgres on purpose: what a sweep promises is that a second run
of the same job finds the lock held and steps aside without running its core.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_core.schemas.temporal import DrainInput, SweepInput
from freezegun import freeze_time
from structlog.testing import capture_logs
from worker.activities._sweep import clock, locked_sweep
from worker.tasks._hardening import advisory_lock

PINNED = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
WALL = datetime(2026, 7, 4, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "input",
    [
        pytest.param(SweepInput(now=PINNED), id="sweep-input"),
        pytest.param(DrainInput(now=PINNED), id="drain-input"),
    ],
)
def test_a_pinned_clock_wins_over_the_wall_clock(input: SweepInput | DrainInput) -> None:
    with freeze_time(WALL):
        assert clock(input) == PINNED


@pytest.mark.parametrize(
    "input",
    [
        pytest.param(None, id="no-input"),
        pytest.param(SweepInput(), id="sweep-input-unpinned"),
        pytest.param(DrainInput(), id="drain-input-unpinned"),
    ],
)
def test_an_unpinned_run_reads_the_wall_clock(input: SweepInput | DrainInput | None) -> None:
    with freeze_time(WALL) as frozen:
        assert clock(input) == WALL
        frozen.move_to(PINNED)
        assert clock(input) == PINNED


async def test_a_free_lock_runs_the_core_and_returns_its_result() -> None:
    ran: list[str] = []

    async def core() -> dict[str, int]:
        ran.append("core")
        return {"done": 3}

    with capture_logs() as logs:
        result = await locked_sweep("test_locked_sweep_free", core, skipped_event="test.skipped")
    assert result == {"done": 3}
    assert ran == ["core"]
    assert not [e for e in logs if e["event"] == "test.skipped"]


async def test_a_held_lock_skips_the_core_and_logs_the_skip() -> None:
    ran: list[str] = []

    async def core() -> int:
        ran.append("core")
        return 1

    async with advisory_lock("test_locked_sweep_held") as held:
        assert held is True
        with capture_logs() as logs:
            result = await locked_sweep(
                "test_locked_sweep_held", core, skipped_event="test.skipped"
            )
    assert result is None
    assert ran == [], "the core never runs while another run holds the lock"
    assert [e["event"] for e in logs if e["event"] == "test.skipped"] == ["test.skipped"]


async def test_a_skip_without_an_event_logs_nothing_of_its_own() -> None:
    async def core() -> int:
        return 1

    async with advisory_lock("test_locked_sweep_quiet") as held:
        assert held is True
        with capture_logs() as logs:
            assert await locked_sweep("test_locked_sweep_quiet", core) is None
    assert {e["event"] for e in logs} <= {"task.skipped_locked"}
