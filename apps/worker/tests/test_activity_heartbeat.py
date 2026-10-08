"""The liveness ticker the long sweeps run their unchanged cores under.

Outside an activity it must do nothing; inside one it must keep an attempt
alive across a heartbeat timeout the core itself would never satisfy. The
control case runs the same slow activity without the ticker and watches the
server time it out, so the passing case is proven to have teeth.

The ticker must also be invisible to the activity's own lifecycle: a normal
exit stops it, an exception from the body comes out unchanged, and a
cancellation aimed at the activity is observed by the activity wherever it
lands — including at the one place the ticker is itself being cancelled and
awaited. Those cases run under the SDK's ``ActivityEnvironment`` (a real
activity context, the SDK's own cancellation delivery, no server), plus one
round trip through a real Worker where the cancel request can only reach the
activity through the ticker's heartbeats.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from _temporal_helpers import wait_until
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, TimeoutError, TimeoutType
from temporalio.testing import ActivityEnvironment
from temporalio.workflow import ActivityCancellationType
from worker.temporal.heartbeat import DEFAULT_INTERVAL_S, heartbeating

_SLOW_S = 2.5
_HEARTBEAT_TIMEOUT = timedelta(seconds=1)
_TICK_S = 0.01
"""A fast tick for the in-process cases, so a few beats fit in milliseconds."""


@activity.defn(name="stub.slow_with_ticker")
async def slow_with_ticker() -> str:
    async with heartbeating(every_s=0.1):
        await asyncio.sleep(_SLOW_S)
    return "done"


@activity.defn(name="stub.slow_without_ticker")
async def slow_without_ticker() -> str:
    await asyncio.sleep(_SLOW_S)
    return "done"


_cancel_observed: list[str] = []
"""What the cancellable stub saw, in order (reset by the test that uses it)."""


@activity.defn(name="stub.cancellable_with_ticker")
async def cancellable_with_ticker() -> str:
    """A core that would run for a long time under the ticker, and records
    whether the activity's cancellation ever reached it."""
    _cancel_observed.append("started")
    try:
        async with heartbeating(every_s=0.1):
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        _cancel_observed.append("cancelled")
        raise
    return "done"


# Unsandboxed like every test stub: the test module is not sandbox-importable.
@workflow.defn(name="stub.heartbeat_driver", sandboxed=False)
class Driver:
    @workflow.run
    async def run(self, with_ticker: bool) -> str:
        return await workflow.execute_activity(
            slow_with_ticker if with_ticker else slow_without_ticker,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn(name="stub.heartbeat_cancel_driver", sandboxed=False)
class CancelDriver:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            cancellable_with_ticker,
            start_to_close_timeout=timedelta(seconds=60),
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
            # The workflow waits for the activity to confirm the cancellation, so
            # the test observes the activity's side of it, not just the workflow's.
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )


async def test_outside_an_activity_the_ticker_is_a_no_op() -> None:
    assert not activity.in_activity()
    before = len(asyncio.all_tasks())
    async with heartbeating(every_s=0.01):
        assert len(asyncio.all_tasks()) == before, "no ticker task without an activity"


@pytest.mark.parametrize("every_s", [0, -1.0], ids=["zero", "negative"])
async def test_a_non_positive_interval_is_refused_before_anything_runs(every_s: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        async with heartbeating(every_s=every_s):
            raise AssertionError("the block must not be entered")


def test_the_default_interval_fits_inside_the_shortest_heartbeat_timeout() -> None:
    """The policy table's shortest heartbeat timeout is two minutes; ticking four
    times inside it means one dropped tick never fails the attempt."""
    assert DEFAULT_INTERVAL_S == 30.0
    assert DEFAULT_INTERVAL_S * 4 <= timedelta(minutes=2).total_seconds()


@pytest.mark.temporal
async def test_the_ticker_keeps_a_slow_core_alive_past_the_heartbeat_timeout(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    async with temporal_worker(workflows=[Driver], activities=[slow_with_ticker]) as running:
        result = await temporal_client.execute_workflow(
            Driver.run,
            True,
            id=f"heartbeat-{request.node.name}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
    assert result == "done"


@pytest.mark.temporal
async def test_without_the_ticker_the_same_core_is_timed_out(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """The control: the assertion above only means something if the server really
    enforces the heartbeat timeout on an attempt that stays silent."""
    async with temporal_worker(workflows=[Driver], activities=[slow_without_ticker]) as running:
        with pytest.raises(WorkflowFailureError) as excinfo:
            await temporal_client.execute_workflow(
                Driver.run,
                False,
                id=f"no-heartbeat-{request.node.name}",
                task_queue=running.task_queue,
                execution_timeout=timedelta(seconds=60),
            )
    activity_error = excinfo.value.cause
    assert isinstance(activity_error, ActivityError)
    timeout = activity_error.cause
    assert isinstance(timeout, TimeoutError)
    assert timeout.type is TimeoutType.HEARTBEAT


# --- the ticker and the activity's own lifecycle ------------------------------------


def _counting_env() -> tuple[ActivityEnvironment, list[int]]:
    """An activity context that counts the ticker's heartbeats."""
    env = ActivityEnvironment()
    beats: list[int] = []
    env.on_heartbeat = lambda *details: beats.append(1)
    return env, beats


async def _settled(beats: list[int]) -> int:
    """The beat count once the ticker has had every chance to tick again."""
    before = len(beats)
    await asyncio.sleep(_TICK_S * 5)
    assert len(beats) == before, "the ticker kept beating after the block ended"
    return before


async def test_a_normal_exit_stops_the_ticker_and_returns_the_bodys_value() -> None:
    env, beats = _counting_env()

    async def body() -> str:
        async with heartbeating(every_s=_TICK_S):
            await asyncio.sleep(_TICK_S * 4)
        return "done"

    assert await env.run(body) == "done"
    assert await _settled(beats) >= 2, "it did beat while the block ran"


async def test_an_exception_from_the_body_propagates_unchanged_and_stops_the_ticker() -> None:
    env, beats = _counting_env()
    boom = RuntimeError("the core failed")

    async def body() -> None:
        async with heartbeating(every_s=_TICK_S):
            await asyncio.sleep(_TICK_S * 2)
            raise boom

    with pytest.raises(RuntimeError) as excinfo:
        await env.run(body)
    assert excinfo.value is boom, "the same exception object, not a wrapper"
    assert excinfo.value.__context__ is None, "nothing from the teardown chained onto it"
    await _settled(beats)


async def test_a_cancellation_during_the_body_reaches_the_activity_and_stops_the_ticker() -> None:
    """The common case: the cancel request lands while the core is running."""
    env, beats = _counting_env()

    async def body() -> None:
        async with heartbeating(every_s=_TICK_S):
            asyncio.get_running_loop().call_later(_TICK_S * 3, env.cancel)
            await asyncio.sleep(60)

    with pytest.raises(asyncio.CancelledError):
        await env.run(body)
    assert await _settled(beats) >= 1


async def test_a_cancellation_landing_in_the_ticker_teardown_still_reaches_the_activity() -> None:
    """The narrow case: the block has ended and the teardown is cancelling and
    awaiting the ticker when the activity itself is cancelled. The ticker's own
    CancelledError must be absorbed there — the activity's must not, or the
    activity runs on as if it had never been cancelled."""
    env, beats = _counting_env()
    ran_on: list[str] = []

    async def body() -> str:
        async with heartbeating(every_s=_TICK_S):
            await asyncio.sleep(_TICK_S * 3)
            # Delivered on the very next loop turn — by which point the block has
            # exited and the teardown is awaiting the cancelled ticker.
            asyncio.get_running_loop().call_soon(env.cancel)
        ran_on.append("past the block")
        return "done"

    with pytest.raises(asyncio.CancelledError):
        await env.run(body)
    assert ran_on == [], "the activity must not run past a cancellation"
    await _settled(beats)


async def test_a_ticker_that_dies_on_its_own_surfaces_its_error_when_the_block_exits() -> None:
    """A heartbeat that cannot be delivered is not a silent stop: the core is
    left to finish, and the failure comes out of the block."""
    env = ActivityEnvironment()

    def _broken(*details: object) -> None:
        raise RuntimeError("heartbeat channel down")

    env.on_heartbeat = _broken
    finished: list[str] = []

    async def body() -> None:
        async with heartbeating(every_s=_TICK_S):
            await asyncio.sleep(_TICK_S * 3)
            finished.append("core done")

    with pytest.raises(RuntimeError, match="heartbeat channel down"):
        await env.run(body)
    assert finished == ["core done"]


@pytest.mark.temporal
async def test_a_cancel_request_reaches_the_activity_through_the_tickers_heartbeats(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """Through a real Worker the server can only tell an activity it is cancelled
    in the response to a heartbeat — which under the ticker is the ticker's. The
    activity must see the cancellation and the workflow must end cancelled."""
    _cancel_observed.clear()
    async with temporal_worker(
        workflows=[CancelDriver], activities=[cancellable_with_ticker]
    ) as running:
        handle = await temporal_client.start_workflow(
            CancelDriver.run,
            id=f"heartbeat-cancel-{request.node.name}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        await wait_until(lambda: "started" in _cancel_observed, what="the activity to start")
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as excinfo:
            await handle.result()
    assert isinstance(excinfo.value.cause, CancelledError)
    assert _cancel_observed == ["started", "cancelled"]
