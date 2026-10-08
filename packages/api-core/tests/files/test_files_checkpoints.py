"""The interleave-checkpoint seam every Files concurrency test orders through.

The self-test the spec asks for: a checkpoint a test arms and never releases
must time out with a message that names it, and the pause must actually force
an order that could not have arisen on its own. No test here sleeps to let the
other side win.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from alkera_core.files.checkpoints import (
    CheckpointKilled,
    Checkpoints,
    CheckpointTimeout,
    NoopCheckpoints,
    PausingCheckpoints,
)

FAST = 0.05
"""Short enough that the timeout cases stay quick; nothing waits on wall time."""


async def test_a_pause_forces_the_order_of_two_coroutines(
    checkpoints: PausingCheckpoints,
) -> None:
    """A parks at "a" until the test releases it, so B always records first."""
    order: list[str] = []

    async def coroutine_a() -> None:
        await checkpoints.reach("a")
        order.append("A")

    async def coroutine_b() -> None:
        order.append("B")

    checkpoints.pause("a")
    parked = asyncio.create_task(coroutine_a())
    await checkpoints.wait_paused("a")

    assert order == []
    await coroutine_b()
    assert order == ["B"], "A must still be parked at its checkpoint"

    checkpoints.release("a")
    await parked

    assert order == ["B", "A"]


async def test_without_the_pause_the_same_two_coroutines_run_in_spawn_order(
    checkpoints: PausingCheckpoints,
) -> None:
    """The negative twin: the order above comes from the pause, not the code."""
    order: list[str] = []

    async def coroutine_a() -> None:
        await checkpoints.reach("a")
        order.append("A")

    async def coroutine_b() -> None:
        order.append("B")

    await asyncio.gather(coroutine_a(), coroutine_b())

    assert order == ["A", "B"]


async def test_an_armed_but_never_released_checkpoint_times_out_naming_itself() -> None:
    checkpoints = PausingCheckpoints(timeout=FAST)
    checkpoints.pause("after_put_before_commit")

    with pytest.raises(CheckpointTimeout) as excinfo:
        await checkpoints.reach("after_put_before_commit")

    assert str(excinfo.value) == (
        "checkpoint 'after_put_before_commit' was never released "
        "(armed by the test, reached by the code)"
    )


async def test_a_released_checkpoint_does_not_time_out() -> None:
    """The negative twin of the timeout: releasing in time lets the code run."""
    checkpoints = PausingCheckpoints(timeout=FAST)
    checkpoints.pause("commit")
    parked = asyncio.create_task(checkpoints.reach("commit"))
    await checkpoints.wait_paused("commit")

    checkpoints.release("commit")

    await parked
    assert checkpoints.reached == ("commit",)


async def test_waiting_for_a_checkpoint_the_code_never_reaches_times_out() -> None:
    checkpoints = PausingCheckpoints(timeout=FAST)
    checkpoints.pause("never_reached")

    with pytest.raises(CheckpointTimeout, match="'never_reached'"):
        await checkpoints.wait_paused("never_reached", timeout=FAST)


async def test_a_killed_checkpoint_raises_where_it_is_reached(
    checkpoints: PausingCheckpoints,
) -> None:
    reached_after = False

    async def scenario() -> None:
        nonlocal reached_after
        await checkpoints.reach("before_commit")
        reached_after = True

    checkpoints.kill("before_commit")

    with pytest.raises(CheckpointKilled, match="'before_commit'"):
        await scenario()

    assert reached_after is False, "the kill must abort the scenario in place"


async def test_a_kill_only_fires_at_the_named_checkpoint(
    checkpoints: PausingCheckpoints,
) -> None:
    checkpoints.kill("after_put")

    await checkpoints.reach("before_put")

    with pytest.raises(CheckpointKilled):
        await checkpoints.reach("after_put")


async def test_an_unarmed_checkpoint_is_free_but_recorded(
    checkpoints: PausingCheckpoints,
) -> None:
    await checkpoints.reach("one")
    checkpoints.reach_sync("two")
    await checkpoints.reach("one")

    assert checkpoints.reached == ("one", "two", "one")


async def test_the_hook_drives_reach_sync_for_a_killed_checkpoint(
    checkpoints: PausingCheckpoints,
) -> None:
    hook: Callable[[str], None] = checkpoints.as_hook()
    checkpoints.kill("after_fsync")

    hook("after_write")

    with pytest.raises(CheckpointKilled, match="'after_fsync'"):
        hook("after_fsync")

    assert checkpoints.reached == ("after_write", "after_fsync")


def test_reach_sync_refuses_to_pause_synchronous_code() -> None:
    """Blocking the loop from sync code would deadlock, so it is an error."""
    checkpoints = PausingCheckpoints(timeout=FAST)
    checkpoints.pause("mid_rename")

    with pytest.raises(RuntimeError, match="'mid_rename'"):
        checkpoints.reach_sync("mid_rename")


async def test_the_production_default_neither_pauses_nor_kills() -> None:
    """The same scenario that a PausingCheckpoints kill aborts runs to the end."""
    finished: list[str] = []

    async def scenario(cp: Checkpoints) -> None:
        await cp.reach("after_put")
        cp.reach_sync("after_fsync")
        finished.append("done")

    armed = PausingCheckpoints(timeout=FAST)
    armed.kill("after_put")
    with pytest.raises(CheckpointKilled):
        await scenario(armed)
    assert finished == []

    await scenario(NoopCheckpoints())

    assert finished == ["done"]


async def test_the_production_default_records_nothing() -> None:
    noop = NoopCheckpoints()

    await noop.reach("after_put")
    noop.as_hook()("after_fsync")

    assert not hasattr(noop, "reached")


async def test_releasing_an_unarmed_checkpoint_is_a_test_bug(
    checkpoints: PausingCheckpoints,
) -> None:
    with pytest.raises(KeyError, match="'never_armed'"):
        checkpoints.release("never_armed")


async def test_a_pause_only_holds_the_next_arrival(
    checkpoints: PausingCheckpoints,
) -> None:
    checkpoints.pause("batch")
    parked = asyncio.create_task(checkpoints.reach("batch"))
    await checkpoints.wait_paused("batch")
    checkpoints.release("batch")
    await parked

    await asyncio.wait_for(checkpoints.reach("batch"), timeout=1.0)

    assert checkpoints.reached == ("batch", "batch")
