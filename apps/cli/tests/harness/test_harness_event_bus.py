"""Tests for `alkera_cli.harness.event_bus.EventBus`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from alkera_cli.harness.event_bus import EventBus
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    PartCreated,
    TextPart,
)

_T = datetime(2026, 5, 26, tzinfo=UTC)


def _chunk(seq: int, text: str) -> AgentMessageChunk:
    return AgentMessageChunk(
        event_id=f"ev_{seq}",
        time=_T,
        session_id="s1",
        message_id="m1",
        part_id="p1",
        sequence=seq,
        text=text,
    )


async def test_publish_to_single_subscriber() -> None:
    bus = EventBus()
    received: list[AgentMessageChunk] = []
    sub = bus.subscribe()

    async def consume() -> None:
        async for ev in sub:
            assert isinstance(ev, AgentMessageChunk)
            received.append(ev)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    await bus.publish(_chunk(0, "hi"))
    await bus.publish(_chunk(1, " there"))
    await asyncio.wait_for(task, timeout=1.0)
    assert [ev.text for ev in received] == ["hi", " there"]
    await bus.close()


async def test_multiple_subscribers_each_receive_every_event() -> None:
    bus = EventBus()
    sub_a, sub_b = bus.subscribe(), bus.subscribe()

    async def collect(sub):
        out = []
        async for ev in sub:
            out.append(ev)
            if len(out) == 3:
                return out

    task_a = asyncio.create_task(collect(sub_a))
    task_b = asyncio.create_task(collect(sub_b))
    for i in range(3):
        await bus.publish(_chunk(i, f"t{i}"))
    a, b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1.0)
    assert [e.sequence for e in a] == [0, 1, 2]
    assert [e.sequence for e in b] == [0, 1, 2]
    await bus.close()


async def test_close_terminates_iterators_cleanly() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    collected: list = []

    async def consume() -> None:
        async for ev in sub:
            collected.append(ev)

    task = asyncio.create_task(consume())
    await bus.publish(_chunk(0, "a"))
    await asyncio.sleep(0.01)
    await bus.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(collected) == 1


async def test_publish_after_close_raises() -> None:
    bus = EventBus()
    await bus.close()
    with pytest.raises(RuntimeError, match="closed"):
        await bus.publish(_chunk(0, "x"))


async def test_slow_subscriber_is_dropped_not_blocking() -> None:
    """A consumer that never reads from its queue should NOT block the
    publisher. After overflow, that consumer drops events; other
    consumers still receive everything."""
    bus = EventBus()
    # First subscriber — slow (never consumes).
    bus.subscribe()  # we never iterate this one
    # Second subscriber — fast.
    fast_sub = bus.subscribe()

    # Fire more events than the queue can hold (default 4096).
    burst = 10
    for i in range(burst):
        await bus.publish(_chunk(i, f"t{i}"))

    received_fast: list = []

    async def consume() -> None:
        async for ev in fast_sub:
            received_fast.append(ev)
            if len(received_fast) == burst:
                return

    await asyncio.wait_for(consume(), timeout=1.0)
    assert [e.sequence for e in received_fast] == list(range(burst))
    await bus.close()


async def test_subscriber_count_tracks_lifecycle() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0
    # subscribe() registers SYNCHRONOUSLY. Keep references alive so
    # GC doesn't unregister between assertions.
    sub_a = bus.subscribe()
    assert bus.subscriber_count == 1
    sub_b = bus.subscribe()
    assert bus.subscriber_count == 2
    # Hold the iterators until close to keep the registration alive.
    _ = (sub_a, sub_b)
    await bus.close()


async def test_persisted_vs_non_persisted_events_both_flow() -> None:
    """The bus carries every event type uniformly — the storage filter
    lives in `Chat.append_event`, not here."""
    bus = EventBus()
    sub = bus.subscribe()
    chunk = _chunk(0, "x")
    finalized = PartCreated(
        event_id="ev_final",
        time=_T,
        session_id="s1",
        part=TextPart(part_id="p1", message_id="m1", time=_T, text="x"),
    )

    collected = []

    async def consume() -> None:
        async for ev in sub:
            collected.append(ev)
            if len(collected) == 2:
                return

    task = asyncio.create_task(consume())
    await bus.publish(chunk)
    await bus.publish(finalized)
    await asyncio.wait_for(task, timeout=1.0)
    assert isinstance(collected[0], AgentMessageChunk)
    assert isinstance(collected[1], PartCreated)
    await bus.close()
