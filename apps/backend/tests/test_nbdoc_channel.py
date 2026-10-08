"""The notebook channel ``nb:<item_id>``: the join snapshot and replay,
ordering, frame-addressed widget events, coalescing, the engine's answers,
and how a widget message's refusal reaches the sender.

Driven through a real :class:`EventHub` exactly as a socket is: the feed takes
every batch in the hub's publish, and the socket hears only what its own
subscription accepted, pumped into it in order. Nothing touches the database:
who may read is the ``decide`` seam, here a mutable rung; a widget message's
decision is the service's (``deliver_comm``, pinned against the database in
``test_nbdoc_socket_wiring.py``), here a refusal the test sets; the
notebook's view is the ``view_of`` seam.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from alkera_core.events import EventHub, EventType, HubEvent
from alkera_core.events.hub import Subscription
from alkera_core.notebooks.channel import NbCommFrame, NbFrame
from alkera_core.notebooks.runs import nb_channel
from alkera_core.schemas.realtime import ErrorFrame, SubscribedFrame
from backend.services.crdt.errors import CrdtError
from backend.services.crdt.registry import Access
from backend.services.notebooks import frames
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import ChannelCursor, NotebookFeed
from backend.services.notebooks.service import CommRefusedError
from backend.services.notebooks.socket import NotebookSocket, coalesce
from pydantic import BaseModel

ORG = uuid.uuid4()
ITEM = uuid.uuid4()
CHANNEL = nb_channel(ITEM)
ME = uuid.uuid4()
SOMEONE = uuid.uuid4()
VIEW = {"token": "1.AA", "cells": [], "output_frame_url": "https://c.example/c/nb-output/ab"}


def _batch(kernel: str, *events: dict[str, Any], state: str | None = None) -> HubEvent:
    return HubEvent(
        lane="durable",
        org_id=ORG,
        type=EventType.NOTEBOOK_EVENT.value,
        entity="notebook",
        entity_id=CHANNEL,
        version=0,
        visibility="org",
        payload={"kernel_id": kernel, "state": state, "events": list(events)},
        channel=CHANNEL,
    )


def _ev(seq: int, kind: str = "cell.status", **rest: Any) -> dict[str, Any]:
    return {"seq": seq, "type": kind, **rest}


@dataclass
class Person:
    """A person's rung as the channel asks it: none, can view, can edit."""

    rung: str = "edit"
    sent: list[BaseModel] = field(default_factory=list)
    comms: list[NbCommFrame] = field(default_factory=list)
    asked: list[uuid.UUID] = field(default_factory=list)
    detached: list[uuid.UUID] = field(default_factory=list)
    #: What the service answers the next widget message with (``None``: carried).
    refusal: CommRefusedError | None = None
    #: Set while a join reads the view, to let a test publish meanwhile.
    reading: asyncio.Event | None = None
    release: asyncio.Event | None = None

    async def decide(self, item_id: uuid.UUID) -> Access:
        if self.rung == "none":
            raise CrdtError("not_found")
        return Access(can_read=True, can_write=self.rung == "edit")

    async def send(self, frame: BaseModel) -> None:
        self.sent.append(frame)

    async def deliver(self, item_id: uuid.UUID, frame: NbCommFrame) -> None:
        if self.refusal is not None:
            raise self.refusal
        self.comms.append(frame)

    async def ask(self, item_id: uuid.UUID) -> None:
        self.asked.append(item_id)

    async def view_of(self, item_id: uuid.UUID) -> dict[str, Any]:
        if self.reading is not None and self.release is not None:
            self.reading.set()
            await self.release.wait()
        return dict(VIEW)

    async def detach(self, item_id: uuid.UUID) -> None:
        self.detached.append(item_id)


@dataclass
class Wire:
    hub: EventHub
    feed: NotebookFeed
    person: Person
    socket: NotebookSocket
    sub: Subscription

    async def pump(self) -> None:
        """Hand the socket every event its subscription accepted, in order,
        as the socket's outbound loop does, then send what it queued."""
        while not self.sub.queue.empty():
            item = self.sub.queue.get_nowait()
            if isinstance(item, HubEvent) and self.socket.wants(item):
                self.socket.outbound(item)
        await self.socket.flush()

    def nb(self) -> list[dict[str, Any]]:
        return [f.event for f in self.person.sent if isinstance(f, NbFrame)]

    def events(self) -> list[dict[str, Any]]:
        return [e for e in self.nb() if e.get("type") != "snapshot"]


def _wire(*, max_queued: int = 256, user: uuid.UUID = ME, peer: str | None = "p:1") -> Wire:
    hub = EventHub()
    feed = NotebookFeed(hub)
    feed.start()
    person = Person()
    socket = NotebookSocket(
        send=person.send,
        feed=feed,
        decide=person.decide,
        deliver_comm=person.deliver,
        view_of=person.view_of,
        user_id=user,
        peer_id=peer,
        ask_snapshot=person.ask,
        detach_frames=person.detach,
        max_queued=max_queued,
    )
    sub = hub.subscribe(lambda event: socket.holds(event.channel), label="socket")
    return Wire(hub, feed, person, socket, sub)


@pytest.mark.asyncio
async def test_a_join_gets_the_snapshot_with_the_view_then_every_later_event_once() -> None:
    """Events before the snapshot are not replayed; those after it are, and
    the live ones that follow continue the sequence without a gap or a
    repeat, including one the box sent twice. Every frame is ``t: nb``."""
    w = _wire()
    w.hub.publish(_batch("k1", _ev(1), _ev(2)))
    w.hub.publish(_batch("k1", _ev(3, "snapshot", cells={"a": "fresh"}), _ev(4), state="idle"))
    w.hub.publish(_batch("k1", _ev(5)))
    await w.socket.subscribe(CHANNEL)
    w.hub.publish(_batch("k1", _ev(5), _ev(6)))
    w.hub.publish(_batch("k1", _ev(7)))
    await w.pump()
    subscribed, snapshot = w.person.sent[:2]
    assert isinstance(subscribed, SubscribedFrame) and subscribed.can_write is True
    assert isinstance(snapshot, NbFrame) and snapshot.t == "nb" and snapshot.channel == CHANNEL
    event = snapshot.event
    assert (event["type"], event["kernel_id"], event["seq"]) == ("snapshot", "k1", 3)
    assert event["view"] == VIEW and event["frames"] == {}
    assert event["cells"] == {"a": "fresh"}
    assert [e["seq"] for e in w.events()] == [4, 5, 6, 7]
    assert all(e["kernel_id"] == "k1" for e in w.events())
    assert w.person.asked == []


@pytest.mark.asyncio
async def test_events_heard_while_the_view_is_read_follow_the_snapshot_once() -> None:
    w = _wire()
    w.hub.publish(_batch("k1", _ev(1, "snapshot"), _ev(2)))
    w.person.reading, w.person.release = asyncio.Event(), asyncio.Event()
    joining = asyncio.create_task(w.socket.subscribe(CHANNEL))
    await w.person.reading.wait()
    w.hub.publish(_batch("k1", _ev(2), _ev(3)))
    await w.pump()
    assert not [e for e in w.nb()]
    w.person.release.set()
    await joining
    await w.pump()
    assert w.nb()[0]["type"] == "snapshot"
    assert [e["seq"] for e in w.events()] == [2, 3]


@pytest.mark.asyncio
async def test_a_join_with_no_snapshot_asks_the_box_for_one() -> None:
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    await w.socket.flush()
    (snapshot,) = w.nb()
    assert snapshot["type"] == "snapshot" and snapshot["kernel_id"] is None
    assert snapshot["view"] == VIEW
    assert w.person.asked == [ITEM]


@pytest.mark.asyncio
async def test_a_restarted_kernel_starts_its_sequence_afresh() -> None:
    w = _wire()
    w.hub.publish(_batch("k1", _ev(1, "snapshot"), _ev(2)))
    await w.socket.subscribe(CHANNEL)
    w.hub.publish(_batch("k2", _ev(1, "snapshot"), _ev(2)))
    await w.pump()
    after_join = w.nb()[1:]
    assert [(e["kernel_id"], e["seq"], e["type"]) for e in after_join] == [
        ("k1", 2, "cell.status"),
        ("k2", 1, "snapshot"),
        ("k2", 2, "cell.status"),
    ]
    assert w.feed.replay(ITEM).kernel_id == "k2"


@pytest.mark.asyncio
async def test_a_person_who_may_not_read_is_refused_and_hears_nothing() -> None:
    w = _wire()
    w.person.rung = "none"
    await w.socket.subscribe(CHANNEL)
    w.hub.publish(_batch("k1", _ev(1)))
    await w.pump()
    (refused,) = w.person.sent
    assert isinstance(refused, ErrorFrame) and refused.code == "not_found"
    assert w.events() == []


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("nb:not-a-uuid", "bad_channel", id="not-an-id"),
        pytest.param(f"nb:{str(ITEM).upper()}", "bad_channel", id="a-second-spelling"),
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_channel_is_refused(raw: str, code: str) -> None:
    w = _wire()
    await w.socket.subscribe(raw)
    (refused,) = w.person.sent
    assert isinstance(refused, ErrorFrame) and refused.code == code


@pytest.mark.parametrize(
    ("frame_peer", "reached"),
    [
        pytest.param("p:1", True, id="narrowed-to-this-socket"),
        pytest.param(None, True, id="any-socket-of-its-owner"),
        pytest.param("p:2", False, id="narrowed-to-another-socket"),
    ],
)
@pytest.mark.asyncio
async def test_a_frame_addressed_event_reaches_only_the_frame_s_socket(
    frame_peer: str | None, reached: bool
) -> None:
    mine = frames.mint(owner=ME, item_id=ITEM, peer_id=frame_peer)
    theirs = frames.mint(owner=SOMEONE, item_id=ITEM, peer_id=None)
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    w.hub.publish(
        _batch(
            "k1",
            _ev(1, "comm.msg", comm_id="c", frame_id=mine, content={"data": {"method": "custom"}}),
            _ev(2, "comm.msg", comm_id="c", frame_id=theirs, content={"data": {"method": "x"}}),
            _ev(3, "cell.status"),
        )
    )
    await w.pump()
    assert [e["seq"] for e in w.events()] == ([1, 3] if reached else [3])


@pytest.mark.asyncio
async def test_the_engine_s_answers_resolve_their_request_and_reach_no_socket() -> None:
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    waiting = w.feed.expect("req-1")
    other = w.feed.expect("req-2")
    w.hub.publish(_batch("k1", _ev(1, "answer", request_id="req-1", result={"opens": []})))
    await w.pump()
    answer = await asyncio.wait_for(waiting, 1)
    assert answer["result"] == {"opens": []}
    assert not other.done()
    assert w.events() == []
    assert w.feed.replay(ITEM).events == []
    w.feed.forget("req-2")
    assert other.cancelled()


def _frame_comm(frame_id: str, **rest: Any) -> NbCommFrame:
    return NbCommFrame(
        channel=CHANNEL, frame_id=frame_id, comm_id="c1", msg_id="m1", content={"x": 1}, **rest
    )


@pytest.mark.parametrize(
    ("refusal", "still_held"),
    [
        pytest.param(
            CommRefusedError("forbidden", "Running a notebook takes Can edit"),
            True,
            id="may-not-run",
        ),
        pytest.param(
            CommRefusedError("not_found", "no such frame"), True, id="not-the-sender-s-frame"
        ),
        pytest.param(
            CommRefusedError("notebook.no_machine", "no machine holds it"), True, id="no-machine"
        ),
        pytest.param(
            CommRefusedError("not_found", "no such notebook", lost=True), False, id="notebook-lost"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_widget_message_is_answered_and_only_a_lost_notebook_drops_the_channel(
    refusal: CommRefusedError, still_held: bool
) -> None:
    """Every message is decided again; a refusal reaches the sender as an
    error frame with the service's code, and the channel goes only when the
    sender may no longer read the notebook (its frames detached with it)."""
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    await w.socket.flush()
    frame = _frame_comm(frames.mint(owner=ME, item_id=ITEM, peer_id="p:1"))
    await w.socket.inbound(frame.model_dump_json())
    w.person.refusal = refusal
    await w.socket.inbound(frame.model_dump_json())
    await asyncio.sleep(0)
    assert [c.msg_id for c in w.person.comms] == ["m1"]
    errors = [(f.code, f.message, f.channel) for f in w.person.sent if isinstance(f, ErrorFrame)]
    assert errors == [(refusal.code, refusal.message, CHANNEL)]
    assert w.socket.holds(CHANNEL) is still_held
    assert w.person.detached == ([] if still_held else [ITEM])


@pytest.mark.asyncio
async def test_a_widget_message_needs_the_channel_and_a_well_formed_body() -> None:
    w = _wire()
    frame = _frame_comm(frames.mint(owner=ME, item_id=ITEM, peer_id=None))
    await w.socket.inbound(frame.model_dump_json())
    await w.socket.inbound('{"t": "nb.comm", "channel": "x"}')
    codes = [f.code for f in w.person.sent if isinstance(f, ErrorFrame)]
    assert codes == ["not_subscribed", "bad_frame"]
    assert w.person.comms == []


@pytest.mark.asyncio
async def test_leaving_a_notebook_detaches_the_socket_s_frames() -> None:
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    assert w.socket.drop(CHANNEL)
    await asyncio.sleep(0)
    assert w.person.detached == [ITEM]
    await w.socket.subscribe(CHANNEL)
    await w.socket.close()
    await asyncio.sleep(0)
    assert w.person.detached == [ITEM, ITEM]


@pytest.mark.asyncio
async def test_recheck_drops_a_channel_the_person_lost() -> None:
    w = _wire()
    await w.socket.subscribe(CHANNEL)
    assert await w.socket.recheck() == []
    w.person.rung = "none"
    assert await w.socket.recheck() == [CHANNEL]
    w.hub.publish(_batch("k1", _ev(1)))
    await w.pump()
    assert w.events() == []


def test_a_frame_id_is_its_owner_s_on_its_notebook_only() -> None:
    minted = frames.mint(owner=ME, item_id=ITEM, peer_id="p:9")
    found = frames.verify(minted, item_id=ITEM)
    assert found is not None and found.owner == ME
    assert frames.verify(minted, item_id=uuid.uuid4()) is None
    tampered = minted.replace(ME.hex, SOMEONE.hex)
    assert frames.verify(tampered, item_id=ITEM) is None
    assert frames.reaches(minted, user_id=ME, peer_id="p:9")
    assert not frames.reaches(minted, user_id=ME, peer_id="p:8")
    assert not frames.reaches(minted, user_id=SOMEONE, peer_id="p:9")
    assert minted.startswith(frames.owner_prefix(owner=ME, peer_id="p:9"))


def _update(comm: str, seq: int, **state: Any) -> dict[str, Any]:
    return _ev(
        seq, "comm.msg", comm_id=comm, content={"data": {"method": "update", "state": state}}
    )


def test_consecutive_updates_of_one_model_merge_and_nothing_else_does() -> None:
    merged = coalesce(
        [
            _update("a", 1, value=1, label="x"),
            _update("a", 2, value=2),
            _update("b", 3, value=9),
            _update("a", 4, value=3),
            _ev(5, "comm.msg", comm_id="a", content={"data": {"method": "custom"}}),
            _update("a", 6, value=4),
        ]
    )
    assert [(e["seq"], e["comm_id"]) for e in merged] == [
        (2, "a"),
        (3, "b"),
        (4, "a"),
        (5, "a"),
        (6, "a"),
    ]
    assert merged[0]["content"]["data"]["state"] == {"value": 2, "label": "x"}


def test_an_update_carrying_buffers_is_never_merged() -> None:
    first = _update("a", 1, value=1)
    second = {**_update("a", 2, value=2), "buffers": ["AA=="]}
    assert coalesce([first, second]) == [first, second]


@pytest.mark.asyncio
async def test_a_slow_socket_is_sent_merged_updates_then_a_resync_when_it_overflows() -> None:
    w = _wire(max_queued=3)
    await w.socket.subscribe(CHANNEL)
    await w.socket.flush()
    w.person.sent.clear()
    # Nothing is sent while these arrive: one queued frame keeps merging.
    for seq in range(1, 6):
        w.hub.publish(_batch("k1", _update("a", seq, value=seq)))
        while not w.sub.queue.empty():
            item = w.sub.queue.get_nowait()
            assert isinstance(item, HubEvent)
            w.socket.outbound(item)
    await w.socket.flush()
    (only,) = w.events()
    assert only["seq"] == 5 and only["content"]["data"]["state"] == {"value": 5}
    w.person.sent.clear()
    other = nb_channel(uuid.uuid4())
    for frame in (
        SubscribedFrame(channel="doc:x:y", can_write=False),
        SubscribedFrame(channel="doc:x:z", can_write=False),
        SubscribedFrame(channel="doc:x:w", can_write=False),
    ):
        w.socket._enqueue(frame)
    w.socket._enqueue(NbFrame(channel=CHANNEL, event=_ev(9)))
    w.socket._enqueue(NbFrame(channel=other, event=_ev(1)))
    await w.socket.flush()
    resyncs = [
        f.channel for f in w.person.sent if isinstance(f, NbFrame) and f.event == {"type": "resync"}
    ]
    assert resyncs == [CHANNEL, other]
    assert [f.channel for f in w.person.sent if isinstance(f, SubscribedFrame)] == [
        "doc:x:y",
        "doc:x:z",
        "doc:x:w",
    ]
    assert not w.socket.holds(CHANNEL)


def test_the_cursor_drops_what_it_has_sent_and_follows_a_new_kernel() -> None:
    cursor = ChannelCursor()
    cursor.start_at("k1", 3)
    assert [e["seq"] for e in cursor.admit("k1", [_ev(2), _ev(3), _ev(4)])] == [4]
    assert [e["seq"] for e in cursor.admit("k1", [_ev(4), _ev(5)])] == [5]
    assert [e["seq"] for e in cursor.admit("k2", [_ev(1)])] == [1]


def test_a_widget_library_offer_resolves_its_module_to_its_entry_file() -> None:
    hub = EventHub()
    feed = NotebookFeed(hub)
    feed.start()
    entry, other = "c" * 64, "d" * 64
    hub.publish(
        _batch(
            "k1",
            _ev(
                1,
                "widget.asset",
                module="bqplot",
                version="0.12.45",
                files=[
                    {"path": "extra.js", "sha256": other},
                    {"path": "index.js", "sha256": entry},
                ],
            ),
        )
    )
    assert feed.module(ITEM, "bqplot", "0.12.45") == entry
    assert feed.offered(ITEM, other) and feed.offered(ITEM, entry)
    assert feed.module(ITEM, "bqplot", "0.12.44") is None
    hub.publish(_batch("k2", _ev(1)))
    assert feed.module(ITEM, "bqplot", "0.12.45") is None


def test_the_feed_remembers_only_assets_the_current_kernel_offered() -> None:
    hub = EventHub()
    feed = NotebookFeed(hub)
    feed.start()
    sha = "a" * 64
    hub.publish(_batch("k1", _ev(1, "widget.asset_offered", sha256=sha)))
    assert feed.offered(ITEM, sha) and not feed.offered(ITEM, "b" * 64)
    hub.publish(_batch("k2", _ev(1)))
    assert not feed.offered(ITEM, sha)
    assert not feed.offered(uuid.uuid4(), sha)


def _caret(item: uuid.UUID, peer: int, cell: str | None, *, user: str = "u1") -> HubEvent:
    channel = f"doc:notebook:{item}"
    body: dict[str, Any] = {
        "envelope": {"payload": {"loro_peer": peer, "user_id": user, "display_name": "Bo"}}
    }
    if cell is not None:
        body["caret_cell"] = cell
    return HubEvent(
        lane="ephemeral",
        org_id=ORG,
        type="doc.crdt_ephemeral",
        entity="doc",
        entity_id=channel,
        version=0,
        visibility="org",
        payload=body,
        channel=channel,
    )


def test_the_caret_board_keeps_each_peer_s_latest_caret_within_the_window() -> None:
    now = [100.0]
    hub = EventHub()
    board = CaretBoard(hub, clock=lambda: now[0])
    board.start()
    hub.publish(_caret(ITEM, 2000, "cellaaaaaa"))
    hub.publish(_caret(ITEM, 2000, "cellbbbbbb"))
    hub.publish(_caret(ITEM, 2001, "cellaaaaaa", user="u2"))
    found = board.present(ITEM, ["cellaaaaaa", "cellbbbbbb"], within=15)
    assert {cell: [c.peer for c in carets] for cell, carets in found.items()} == {
        "cellaaaaaa": [2001],
        "cellbbbbbb": [2000],
    }
    assert board.present(ITEM, ["cellaaaaaa"], within=15, exclude_user="u2") == {}
    hub.publish(_caret(ITEM, 2000, None))
    assert "cellbbbbbb" not in board.present(ITEM, ["cellbbbbbb"], within=15)
    now[0] += 16
    assert board.present(ITEM, ["cellaaaaaa"], within=15) == {}
    assert board.present(uuid.uuid4(), ["cellaaaaaa"], within=15) == {}


def test_the_caret_board_reads_the_keys_the_gateway_writes() -> None:
    from backend.services.crdt import gateway
    from backend.services.notebooks import carets

    assert carets.CARET_CELL_KEY == gateway.CARET_CELL_KEY
    assert carets.CARET_EVENT_TYPE == gateway.CRDT_EPHEMERAL_EVENT_TYPE
