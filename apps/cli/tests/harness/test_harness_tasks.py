"""``stop_task`` — ending a background task without losing the caller's own cancellation.

``await task`` raises ``CancelledError`` both when the task ended by being
cancelled and when the CALLER was cancelled while waiting, and every
``suppress`` around such an await conflated the two: a deadline on a shutdown
or a test's timeout was eaten and the caller went on as if nothing had
happened. And one ``cancel()`` is not proof the task was ever told — a
cancellation delivered at a wakeup is swallowed by whatever catches it there,
and a stop that then only waits never ends. These pin the distinction, the
re-ask, the bound, and the outcomes it consumes.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from alkera_cli.harness.tasks import RECANCEL_INTERVAL_S, stop_task


async def _stubborn(obey: asyncio.Event, reached: asyncio.Event | None = None) -> None:
    """Ignore every cancellation until told to obey — a stand-in for work wedged
    in a wait the cancellation cannot reach."""
    if reached is not None:
        reached.set()
    while not obey.is_set():
        try:
            await obey.wait()
        except asyncio.CancelledError:
            continue
    raise asyncio.CancelledError


async def _ends_a_little_after_its_cancel(delay: float) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(delay)
        raise


async def _absorbs_one_cancellation_then_waits_forever(go: asyncio.Event) -> None:
    """Swallow the first cancellation, the way an async generator ending its own
    cleanup does, then go back to waiting with nobody holding it."""
    try:
        await go.wait()
    except asyncio.CancelledError:
        pass
    await asyncio.Event().wait()


async def _fails_on_the_way_out() -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise RuntimeError("the cleanup broke") from None


async def test_a_task_already_finished_is_reported_ended_without_being_cancelled() -> None:
    async def done() -> str:
        return "ok"

    task = asyncio.create_task(done())
    await task
    assert await stop_task(task) is True
    assert not task.cancelled() and task.result() == "ok"


async def test_a_task_that_ends_on_its_cancellation_is_ended() -> None:
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    assert await stop_task(task) is True
    assert task.cancelled()


async def test_a_task_that_takes_a_moment_to_end_is_waited_for() -> None:
    task = asyncio.create_task(_ends_a_little_after_its_cancel(0.05))
    await asyncio.sleep(0)
    assert await stop_task(task) is True
    assert task.done() and task.cancelled()


async def test_a_task_failing_on_the_way_out_is_ended_and_its_error_is_kept() -> None:
    task = asyncio.create_task(_fails_on_the_way_out())
    await asyncio.sleep(0)
    assert await stop_task(task) is True
    assert isinstance(task.exception(), RuntimeError)


async def test_a_task_that_ignores_its_cancellation_is_abandoned_after_the_timeout() -> None:
    obey = asyncio.Event()
    task = asyncio.create_task(_stubborn(obey))
    await asyncio.sleep(0)
    started = asyncio.get_running_loop().time()
    assert await stop_task(task, grace=0.05) is False
    assert asyncio.get_running_loop().time() - started < 1.0, "the wait outlived its timeout"
    assert not task.done(), "an abandoned task is left running, not pretended ended"
    assert task.cancelling(), "an abandoned task was still asked to stop"
    obey.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_a_task_that_swallows_the_first_cancellation_is_asked_again() -> None:
    """One ``cancel()`` is not proof the task was ever told.

    A cancellation that arrives while the task is not suspended on a cancellable
    future is only recorded, to be delivered the next time the task runs — and a
    task that catches it there absorbs it and waits on. Asked once and then only
    waited for, the stop never ends: the service's event-stream loop absorbed
    exactly this and its shutdown sat on it for as long as the box ran.
    """
    go = asyncio.Event()
    task = asyncio.create_task(_absorbs_one_cancellation_then_waits_forever(go))
    await asyncio.sleep(0)  # the task is now suspended inside `go.wait()`
    # Resolving the wait leaves the first cancel nothing to interrupt: it is recorded
    # and delivered on the wakeup, which is where the task swallows it.
    go.set()
    async with asyncio.timeout(RECANCEL_INTERVAL_S * 3):
        assert await stop_task(task, grace=RECANCEL_INTERVAL_S * 5) is True
    assert task.cancelled()


@pytest.mark.parametrize(
    "grace", [pytest.param(None, id="no-bound"), pytest.param(5.0, id="bound")]
)
async def test_the_callers_own_cancellation_is_not_mistaken_for_the_tasks(
    grace: float | None,
) -> None:
    obey = asyncio.Event()
    task = asyncio.create_task(_stubborn(obey))
    await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await stop_task(task, grace=grace)
    assert not task.done()
    obey.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task
