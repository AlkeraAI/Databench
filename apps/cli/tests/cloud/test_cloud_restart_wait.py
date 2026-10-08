"""How long a supervised restart in place waits: minutes for a background
job, never the drain ceiling, and only while a job is actually running."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest
from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.cloud.restart_wait import RestartWait, job_ceiling, wait_for_in_flight
from alkera_cli.supervisor.org_resume import RESTART_JOB_CEILING_SECONDS
from alkera_core.compute.liveness import DRAIN_CEILING_SECONDS, RESTART_DRAIN_CEILING_SECONDS
from freezegun import freeze_time

JOB = ChatActivity.RUNNING_JOB
TURN = ChatActivity.WORKING


async def _waited(
    script: Callable[[float], Sequence[ChatActivity]], *, drain_ceiling: float
) -> float:
    """How long the wait took on a frozen clock that only its sleeps move;
    ``script`` says what the box holds at each moment since the start."""
    with freeze_time(datetime(2026, 9, 1, tzinfo=UTC), real_asyncio=True) as frozen:
        started = time.monotonic()

        async def sleep(seconds: float) -> None:
            frozen.tick(seconds)

        await wait_for_in_flight(
            lambda: script(time.monotonic() - started),
            clock=time.monotonic,
            sleep=sleep,
            drain_ceiling=drain_ceiling,
            poll=1.0,
        )
        return time.monotonic() - started


async def test_a_job_seen_once_does_not_hold_the_restart_open_after_it_ends() -> None:
    """The wait switched to the six-hour ceiling the first time it saw a job
    and kept it: any chat working on a later poll held the restart for hours,
    the box taking nothing new and the release it was restarting for not
    live."""

    def box(at: float) -> Sequence[ChatActivity]:
        if at < 100:
            return [JOB, TURN]
        return [TURN]  # the job ended; another chat keeps working

    waited = await _waited(box, drain_ceiling=DRAIN_CEILING_SECONDS)

    assert waited == pytest.approx(100 + RESTART_DRAIN_CEILING_SECONDS, abs=1.0)


async def test_a_job_that_keeps_running_is_waited_for_its_ceiling_not_the_drain_ceiling() -> None:
    waited = await _waited(lambda at: [JOB], drain_ceiling=DRAIN_CEILING_SECONDS)
    assert waited == pytest.approx(RESTART_JOB_CEILING_SECONDS, abs=1.0)
    assert waited < DRAIN_CEILING_SECONDS / 10


async def test_the_turn_on_a_job_s_result_gets_the_short_ceiling_from_the_job_s_end() -> None:
    def box(at: float) -> Sequence[ChatActivity]:
        if at < 200:
            return [JOB]
        return [TURN] if at < 215 else []

    waited = await _waited(box, drain_ceiling=DRAIN_CEILING_SECONDS)
    assert waited == pytest.approx(215, abs=1.0)


@pytest.mark.parametrize(
    ("drain_ceiling", "expected"),
    [
        pytest.param(120.0, 120.0, id="a-short-drain-ceiling-bounds-it"),
        pytest.param(0.0, float(RESTART_DRAIN_CEILING_SECONDS), id="never-below-the-turn-wait"),
        pytest.param(DRAIN_CEILING_SECONDS, float(RESTART_JOB_CEILING_SECONDS), id="default"),
    ],
)
def test_the_job_ceiling(drain_ceiling: float, expected: float) -> None:
    assert job_ceiling(drain_ceiling) == expected


async def test_turns_alone_get_the_short_ceiling() -> None:
    waited = await _waited(lambda at: [TURN], drain_ceiling=DRAIN_CEILING_SECONDS)
    assert waited == pytest.approx(RESTART_DRAIN_CEILING_SECONDS, abs=1.0)


async def test_nothing_running_is_no_wait() -> None:
    assert await _waited(lambda at: [], drain_ceiling=DRAIN_CEILING_SECONDS) == 0


async def _outcome(
    script: Callable[[float], Sequence[ChatActivity]], caplog: pytest.LogCaptureFixture
) -> tuple[RestartWait, list[str]]:
    with (
        caplog.at_level(logging.INFO, logger="alkera_cli.cloud.restart_wait"),
        freeze_time(datetime(2026, 9, 1, tzinfo=UTC), real_asyncio=True) as frozen,
    ):
        started = time.monotonic()

        async def sleep(seconds: float) -> None:
            frozen.tick(seconds)

        outcome = await wait_for_in_flight(
            lambda: script(time.monotonic() - started),
            clock=time.monotonic,
            sleep=sleep,
            drain_ceiling=DRAIN_CEILING_SECONDS,
            poll=1.0,
        )
    return outcome, [r.getMessage() for r in caplog.records]


async def test_a_wait_that_runs_out_logs_its_bound_and_the_chats_it_cuts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The box journal said only "restarting with 1 chat(s) still working":
    not how long the restart had waited, nor that the wait had a bound."""
    outcome, lines = await _outcome(lambda at: [TURN, TURN], caplog)

    assert outcome == RestartWait(
        held=2, waited=float(RESTART_DRAIN_CEILING_SECONDS), still_working=2, on_a_job=0
    )
    assert lines[0] == (
        f"restart: waiting up to {RESTART_DRAIN_CEILING_SECONDS} s for 2 chat(s) "
        "still working to finish"
    )
    assert lines[-1] == (
        "restarting with 2 chat(s) still working, 0 of them on a job, after "
        f"{RESTART_DRAIN_CEILING_SECONDS} s: the next process ends or resumes their turns"
    )


async def test_a_wait_whose_chats_finish_logs_that_they_did(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome, lines = await _outcome(lambda at: [TURN] if at < 4 else [], caplog)

    assert outcome == RestartWait(held=1, waited=4.0, still_working=0, on_a_job=0)
    assert lines[-1] == "restart: all 1 chat(s) finished their work in 4 s"


async def test_a_job_s_bound_is_the_one_logged(caplog: pytest.LogCaptureFixture) -> None:
    _, lines = await _outcome(lambda at: [JOB] if at < 2 else [], caplog)

    assert lines[0].startswith(f"restart: waiting up to {RESTART_JOB_CEILING_SECONDS} s ")


async def test_nothing_running_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    outcome, lines = await _outcome(lambda at: [ChatActivity.AWAITING_USER], caplog)

    assert outcome == RestartWait(held=0, waited=0.0, still_working=0, on_a_job=0)
    assert lines == []
