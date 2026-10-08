"""A notebook channel never passes a partial history off as whole.

The replica's replay ring keeps a bounded number of events after the last
snapshot, and a batch can be lost on its way to a replica. Either way the
events a joiner would get have a hole: the ring then says it is not whole,
and the box is asked for a snapshot (once, while none comes), which brings
every socket on the notebook whole again.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest
from backend.services.notebooks.feed import (
    RING_EVENTS,
    SNAPSHOT_ASK_SECONDS,
    ChannelCursor,
    NotebookFeed,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.test_nbdoc_channel import CHANNEL, ITEM, _batch, _ev, _wire


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _settle(w: Any) -> None:
    """Send what the socket queued and let the snapshot ask it started run."""
    await w.socket.flush()
    for _ in range(5):
        await asyncio.sleep(0)


def _feed() -> tuple[NotebookFeed, Clock]:
    clock = Clock()
    return NotebookFeed(clock=clock), clock


def test_a_ring_that_dropped_an_event_to_make_room_is_not_whole() -> None:
    feed, _ = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot")))
    feed.observe(_batch("k1", *(_ev(seq) for seq in range(2, RING_EVENTS + 2))))
    assert feed.replay(ITEM).whole is True
    feed.observe(_batch("k1", _ev(RING_EVENTS + 2)))
    replay = feed.replay(ITEM)
    assert replay.whole is False
    assert replay.events[0]["seq"] == 3


def test_a_lost_batch_leaves_a_hole_and_a_snapshot_mends_it() -> None:
    feed, _ = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot"), _ev(2)))
    feed.observe(_batch("k1", _ev(5)))
    assert feed.replay(ITEM).whole is False
    feed.observe(_batch("k1", _ev(6, "snapshot"), _ev(7)))
    replay = feed.replay(ITEM)
    assert (replay.whole, replay.seq, [e["seq"] for e in replay.events]) == (True, 6, [7])


def test_a_new_kernel_starts_whole() -> None:
    feed, _ = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot"), _ev(9)))
    assert feed.replay(ITEM).whole is False
    feed.observe(_batch("k2", _ev(1, "snapshot"), _ev(2)))
    assert feed.replay(ITEM).whole is True


def test_a_snapshot_is_asked_for_once_while_none_comes() -> None:
    feed, clock = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot"), _ev(4)))
    assert feed.wants_snapshot(ITEM) is True
    assert feed.wants_snapshot(ITEM) is False
    clock.now += SNAPSHOT_ASK_SECONDS - 0.1
    assert feed.wants_snapshot(ITEM) is False
    clock.now += 0.2
    assert feed.wants_snapshot(ITEM) is True
    feed.observe(_batch("k1", _ev(5, "snapshot")))
    clock.now += SNAPSHOT_ASK_SECONDS * 2
    assert feed.wants_snapshot(ITEM) is False


def test_a_whole_ring_with_a_snapshot_asks_for_nothing() -> None:
    feed, _ = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot"), _ev(2), _ev(3)))
    assert feed.wants_snapshot(ITEM) is False


def test_the_cursor_reports_a_hole_once() -> None:
    cursor = ChannelCursor()
    cursor.start_at("k1", 1)
    assert [e["seq"] for e in cursor.admit("k1", [_ev(2), _ev(3)])] == [2, 3]
    assert cursor.take_gap() is False
    assert [e["seq"] for e in cursor.admit("k1", [_ev(6)])] == [6]
    assert cursor.take_gap() is True
    assert cursor.take_gap() is False
    cursor.admit("k1", [_ev(9, "snapshot")])
    assert cursor.take_gap() is False


@pytest.mark.asyncio
async def test_joining_a_ring_with_a_hole_asks_the_box_for_a_snapshot() -> None:
    w = _wire()
    w.hub.publish(_batch("k1", _ev(1, "snapshot"), _ev(2)))
    w.hub.publish(_batch("k1", _ev(7)))
    await w.socket.subscribe(CHANNEL)
    await _settle(w)
    assert w.person.asked == [ITEM]


@pytest.mark.asyncio
async def test_a_batch_a_joined_socket_never_heard_asks_for_a_snapshot() -> None:
    w = _wire()
    w.hub.publish(_batch("k1", _ev(1, "snapshot"), _ev(2)))
    await w.socket.subscribe(CHANNEL)
    assert w.person.asked == []
    w.socket.outbound(_batch("k1", _ev(5)))
    await _settle(w)
    assert w.person.asked == [ITEM]


@settings(max_examples=200, deadline=None)
@given(
    count=st.integers(min_value=1, max_value=RING_EVENTS + 300),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    drop=st.floats(min_value=0.0, max_value=0.2),
)
def test_a_replay_that_says_it_is_whole_holds_every_event_since_its_snapshot(
    count: int, seed: int, drop: float
) -> None:
    """The gate: whatever batches are lost on the way to the replica, a
    replay never claims to be whole while an event after its snapshot is
    missing from it."""
    rng = random.Random(seed)
    feed, _ = _feed()
    feed.observe(_batch("k1", _ev(1, "snapshot")))
    seq, heard = 1, 1
    while seq < count + 1:
        size = rng.randint(1, 40)
        batch = [_ev(s) for s in range(seq + 1, seq + 1 + size)]
        seq += size
        if rng.random() < drop:
            continue
        feed.observe(_batch("k1", *batch))
        heard = seq
    replay = feed.replay(ITEM)
    got: list[Any] = [e["seq"] for e in replay.events]
    # A lost batch is known only once a later one arrives, so wholeness is
    # judged up to the last event the replica heard.
    if replay.whole:
        assert got == list(range(replay.seq + 1, heard + 1))
