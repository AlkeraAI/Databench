"""The notebook channel on a real person's socket, and a notebook request on
a real box's socket.

A person's :class:`SocketSession` (the one ``run_socket`` builds) subscribes
to ``nb:<item_id>`` through its own inbound frames, hears a notebook batch
through its own outbound loop only while it holds the channel, and loses the
channel on the tick once the notebook's own decision stops admitting it (its
widget messages are pinned against a real notebook in
``test_nbdoc_socket_comm.py``). A notebook request the
transport writes reaches the box's socket as a ``machine.request`` frame only
when the box holds its own machine channel.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alkera_core.auth import SessionClaims, encode_session_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventHub, EventType, HubEvent
from alkera_core.models import User
from alkera_core.notebooks.runs import nb_channel
from alkera_core.schemas.realtime.machine import NotebookMachineRequest, machine_channel
from alkera_notebook.engine.models import KernelInfo, NotebookView, Settings
from backend.authz import decide_on_record
from backend.services.crdt.errors import CrdtError
from backend.services.crdt.registry import Access, DocRef
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from backend.services.notebooks.socket import nb_reconsider
from backend.services.notebooks.transport import KernelHost, MachineChannelTransport
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import EntitlementRef, load_entitlements
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import WebSocket
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio

ITEM = uuid.uuid4()
CHANNEL = nb_channel(ITEM)


@dataclass
class _End:
    """The socket's far end: what the server sends is kept; what the client
    sends is read from ``inbox``."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    inbox: list[str] = field(default_factory=list)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int, reason: str = "") -> None:
        return None

    async def iter_text(self) -> Any:
        for text in self.inbox:
            yield text


@dataclass
class _Lane:
    """The CRDT lane as the channel asks it: the notebook's own decision."""

    readable: bool = True
    writable: bool = True
    asked: list[DocRef] = field(default_factory=list)

    async def access(self, db: Any, ref: DocRef, **_: Any) -> Access:
        self.asked.append(ref)
        if not self.readable:
            raise CrdtError("not_found")
        return Access(can_read=True, can_write=self.writable)


@dataclass
class _Transport:
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(
        self, host: KernelHost, op: Any, body: Any, *, request_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        self.sent.append((op, dict(body)))
        return uuid.uuid4()


async def _person(org: OrgWithAdmin, lane: _Lane, service: NotebookService) -> SocketSession:
    _, claims = encode_session_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id, platform_role=None
    )
    async with AsyncSessionLocal() as db:
        user = await db.get(User, org.admin_id)
        assert user is not None
        ref = EntitlementRef(await load_entitlements(db, user, org_id=org.org_id))
        db.expunge(user)
    runtime = SimpleNamespace(crdt=lane, notebooks=service)
    return SocketSession(
        websocket=cast(WebSocket, _End()),
        user=user,
        claims=cast(SessionClaims, claims),
        peer_id="p-nb",
        runtime=cast(RealtimeRuntime, runtime),
        registry=cast(DocRegistry, None),
        ref=ref,
    )


def _batch(org_id: uuid.UUID, seq: int) -> HubEvent:
    return HubEvent(
        lane="durable",
        org_id=org_id,
        type=EventType.NOTEBOOK_EVENT.value,
        entity="notebook",
        entity_id=CHANNEL,
        version=seq,
        visibility="org",
        payload={"kernel_id": "k1", "state": None, "events": [{"seq": seq, "type": "x"}]},
        channel=CHANNEL,
    )


async def _emptied(queue: asyncio.Queue[Any]) -> None:
    for _ in range(1000):
        if queue.empty():
            return
        await asyncio.sleep(0.001)


async def _drain(socket: SocketSession, events: list[HubEvent]) -> None:
    """Run the socket's own outbound loop over ``events`` (those its
    subscription accepted), then let its notebook sender send."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for event in events:
        if socket._accept_event(event):
            queue.put_nowait(event)
    loop = asyncio.create_task(socket._outbound(SimpleNamespace(queue=queue)))
    await asyncio.wait_for(_emptied(queue), timeout=5)
    await asyncio.sleep(0.05)
    loop.cancel()
    assert socket.notebooks is not None
    await socket.notebooks.flush()


async def test_a_person_s_socket_holds_hears_and_writes_the_notebook_channel(
    org_admin: OrgWithAdmin,
) -> None:
    hub = EventHub()
    feed = NotebookFeed(hub)
    feed.start()
    transport = _Transport()
    service = NotebookService(
        transport=transport, feed=feed, carets=CaretBoard(), decide=decide_on_record
    )

    async def view_for(**_: Any) -> NotebookView:
        return NotebookView(
            path="analysis.alknb.py",
            token="1.AA",
            settings=Settings(),
            kernel=KernelInfo(state="absent", env=None, reactivity="autorun"),
            cells=[],
        )

    service.view_for = view_for  # type: ignore[method-assign]
    lane = _Lane()
    socket = await _person(org_admin, lane, service)
    end = cast(_End, socket.websocket)
    hub.publish(_batch(org_admin.org_id, 1))
    end.inbox = [json.dumps({"t": "subscribe", "channel": CHANNEL})]
    await socket._inbound()
    assert socket.notebooks is not None
    await socket.notebooks.flush()
    later = [_batch(org_admin.org_id, 2), _batch(uuid.uuid4(), 3)]
    await _drain(socket, later)
    kinds = [frame["t"] for frame in end.sent]
    assert kinds == ["subscribed", "nb", "nb", "nb"]
    snapshot = end.sent[1]["event"]
    assert snapshot["type"] == "snapshot" and snapshot["view"]["token"] == "1.AA"
    assert [f["event"]["seq"] for f in end.sent[2:]] == [1, 2]
    assert lane.asked == [DocRef(org_admin.org_id, "notebook", str(ITEM))]

    # The tick re-decides the channel; once the notebook is out of reach the
    # channel is gone and nothing more is heard.
    lane.readable = False
    await nb_reconsider(socket.notebooks)
    assert end.sent[-1]["t"] == "error" and end.sent[-1]["code"] == "not_found"
    count = len(end.sent)
    await _drain(socket, [_batch(org_admin.org_id, 4)])
    assert len(end.sent) == count


async def test_a_socket_in_a_process_with_no_notebooks_refuses_the_channel(
    org_admin: OrgWithAdmin,
) -> None:
    socket = await _person(org_admin, _Lane(), cast(NotebookService, None))
    assert socket.notebooks is None
    end = cast(_End, socket.websocket)
    end.inbox = [json.dumps({"t": "subscribe", "channel": CHANNEL})]
    await socket._inbound()
    assert [(f["t"], f.get("code")) for f in end.sent] == [("error", "not_found")]


async def test_a_notebook_request_reaches_only_the_box_holding_its_machine_channel(
    org_admin: OrgWithAdmin,
) -> None:
    """The transport writes the request to the outbox addressed to the
    machine's channel; read back as the hub reads it, a socket holding that
    channel frames it as the box's request, and one that does not frames
    nothing."""
    from alkera_core.models import EventOutbox
    from sqlalchemy import select

    host = KernelHost(
        org_id=org_admin.org_id, drive_id=uuid.uuid4(), item_id=ITEM, machine_id="m-1"
    )
    request_id = await MachineChannelTransport().send(host, "run", {"run_id": "r1"})
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(EventOutbox).where(
                    EventOutbox.type == EventType.NOTEBOOK_EVENT.value,
                    EventOutbox.entity_id == machine_channel("m-1"),
                    EventOutbox.org_id == org_admin.org_id,
                )
            )
        ).scalar_one()
    event = HubEvent.from_outbox(row)
    assert event.channel == machine_channel("m-1")
    socket = await _person(org_admin, _Lane(), cast(NotebookService, None))
    assert socket._frame_for(event) is None
    socket.machine_channels.add(machine_channel("m-1"))
    frame = socket._frame_for(event)
    assert isinstance(frame, NotebookMachineRequest)
    assert (frame.request_id, frame.op, frame.item_id, frame.body) == (
        request_id,
        "run",
        ITEM,
        {"run_id": "r1"},
    )
