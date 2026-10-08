"""The reaper the root conftest runs after pytest-timeout ends an async test.

Every case drives a private loop of its own, never the session loop: the
mechanism under test is exactly "the loop is not running and a task from an
earlier test is parked on it", which the session loop must never be put into
on purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator

import pytest
from alkera_core.utils.abandoned_tasks import (
    Reaped,
    format_report,
    is_pytest_timeout,
    pending_tasks,
    reap_abandoned_tasks,
)


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    private = asyncio.new_event_loop()
    try:
        yield private
    finally:
        with contextlib.suppress(Exception):
            private.run_until_complete(private.shutdown_asyncgens())
        private.close()


def _tick(loop: asyncio.AbstractEventLoop) -> None:
    """Let every scheduled task run up to its first await."""
    loop.run_until_complete(asyncio.sleep(0))


@contextlib.asynccontextmanager
async def _served(closed: list[str], name: str) -> AsyncIterator[asyncio.Task[None]]:
    """A stand-in for ``serve_app``: a child task the test task owns, closed on exit."""

    async def serve() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(f"{name}-task")

    child = asyncio.get_running_loop().create_task(serve(), name=f"{name}-task")
    try:
        yield child
    finally:
        child.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await child
        closed.append(name)


def test_a_task_the_test_left_parked_is_cancelled_and_its_cleanup_runs(
    loop: asyncio.AbstractEventLoop,
) -> None:
    closed: list[str] = []

    async def test_body() -> None:
        async with _served(closed, "server"):
            await asyncio.Event().wait()

    before = pending_tasks(loop)
    parked = loop.create_task(test_body(), name="the-test")
    _tick(loop)
    assert not parked.done() and closed == [], "the test body is parked mid-await"

    reaped = reap_abandoned_tasks(loop, before, drain_seconds=2.0)

    assert parked.cancelled()
    assert closed == ["server-task", "server"], "the async-with exit ran, child first"
    assert reaped == Reaped(cancelled=("server-task", "the-test"), survivors=())
    assert not pending_tasks(loop), "nothing of the test survives on the loop"


def test_a_task_alive_before_the_test_is_not_touched(loop: asyncio.AbstractEventLoop) -> None:
    async def listener() -> None:
        await asyncio.Event().wait()

    fixture_task = loop.create_task(listener(), name="session-listener")
    _tick(loop)
    before = pending_tasks(loop)
    assert fixture_task in before

    async def test_body() -> None:
        await asyncio.Event().wait()

    parked = loop.create_task(test_body(), name="the-test")
    _tick(loop)

    reaped = reap_abandoned_tasks(loop, before, drain_seconds=2.0)

    assert reaped.cancelled == ("the-test",)
    assert not fixture_task.done() and not fixture_task.cancelled()
    assert parked.cancelled()
    fixture_task.cancel()
    _tick(loop)


def test_a_task_that_ignores_cancellation_is_named_not_waited_on_forever(
    loop: asyncio.AbstractEventLoop,
) -> None:
    # Cancellation never ends this task; only the release does. The test holds
    # the release so the loop can still be closed cleanly afterwards — awaiting
    # a cancelled task that swallows the cancel would itself never return.
    release = asyncio.Event()

    async def immortal() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    before = pending_tasks(loop)
    zombie = loop.create_task(immortal(), name="cloud-mirror-machine")
    _tick(loop)

    started = time.monotonic()
    reaped = reap_abandoned_tasks(loop, before, drain_seconds=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the drain is bounded by drain_seconds, took {elapsed:.1f}s"
    assert reaped.cancelled == ()
    assert len(reaped.survivors) == 1
    (survivor,) = reaped.survivors
    assert survivor.startswith("cloud-mirror-machine parked at ")
    assert survivor.endswith(f":{immortal.__code__.co_firstlineno + 3}"), survivor
    assert not zombie.done(), "a survivor is reported, not pretended away"
    report = format_report(reaped, drain_seconds=0.3)
    assert "STILL PENDING after 0.3s" in report and "cloud-mirror-machine" in report
    release.set()
    loop.run_until_complete(zombie)
    assert zombie.done() and not zombie.cancelled()


def test_nothing_parked_is_a_no_op_and_the_loop_stays_usable(
    loop: asyncio.AbstractEventLoop,
) -> None:
    before = pending_tasks(loop)

    reaped = reap_abandoned_tasks(loop, before, drain_seconds=1.0)

    assert reaped == Reaped((), ()) and not reaped
    assert loop.run_until_complete(asyncio.sleep(0, result="still runs")) == "still runs"


def test_a_closed_loop_is_reported_empty_rather_than_run() -> None:
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    assert pending_tasks(closed_loop) == frozenset()
    assert reap_abandoned_tasks(closed_loop, (), drain_seconds=1.0) == Reaped((), ())


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(
            pytest.fail.Exception("Timeout (>400.0s) from pytest-timeout"),
            True,
            id="the-failure-pytest-timeout-raises",
        ),
        pytest.param(
            pytest.fail.Exception("the box never answered"),
            False,
            id="an-ordinary-pytest-fail",
        ),
        pytest.param(
            AssertionError("Timeout (>400.0s) from pytest-timeout"),
            False,
            id="an-assertion-that-quotes-the-message",
        ),
        pytest.param(
            TimeoutError("Timeout (>400.0s) from pytest-timeout"),
            False,
            id="a-timeout-the-test-raised-itself",
        ),
    ],
)
def test_only_pytest_timeouts_own_failure_counts_as_an_abandoned_test(
    exc: BaseException, expected: bool
) -> None:
    assert is_pytest_timeout(exc) is expected


def test_the_report_names_every_task_and_what_became_of_it() -> None:
    reaped = Reaped(
        cancelled=("the-test", "uvicorn-test-server:1"), survivors=("beat parked at x:1",)
    )

    report = format_report(reaped, drain_seconds=15)

    assert report.splitlines() == [
        "cancelled: the-test",
        "cancelled: uvicorn-test-server:1",
        "STILL PENDING after 15s (ignores cancellation): beat parked at x:1",
    ]
