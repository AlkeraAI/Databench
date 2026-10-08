"""A widget message on the notebook channel is decided as POST ``.../comm``
decides it, against real Postgres and real Files.

A person's (or an agent's) real :class:`SocketSession` holds ``nb:<item_id>``
and sends ``nb.comm`` through its own inbound loop; the real
:class:`NotebookService` admits the notebook through the Files policy, decides
``notebook.run`` on the same facts the route reads (the caller's rung, the
lease, the agent's chat) and leaves the ``authz.decision`` row, then checks the
frame is the sender's on this notebook. Only the box transport is recorded,
and the channel's own read decision (the CRDT lane) is a stand-in that admits,
since joining is not what is under test here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alkera_core.auth import SessionClaims, encode_session_token
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventHub
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.runs import nb_channel
from backend.authz import decide_on_record
from backend.services.crdt.registry import Access, DocRef
from backend.services.notebooks import frames
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import SOCKET_METHOD, NotebookService
from backend.services.notebooks.transport import KernelHost
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import EntitlementRef
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import WebSocket
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fastapi_app
from tests.crdt.crdt_world import Person
from tests.crdt.file_world import FileWorld, file_world
from tests.files._boxes import registered_box
from tests.files._live_holder import MockHolder

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

NOTEBOOK = b"import marimo\napp = marimo.App()\n"


@dataclass
class _End:
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
    """The channel's read decision, admitting: joining is not under test."""

    async def access(self, db: Any, ref: DocRef, **_: Any) -> Access:
        return Access(can_read=True, can_write=True)


@dataclass
class _Transport:
    sent: list[tuple[KernelHost, str, dict[str, Any]]] = field(default_factory=list)

    async def send(
        self, host: KernelHost, op: Any, body: Any, *, request_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        self.sent.append((host, op, dict(body)))
        return uuid.uuid4()

    def comms(self) -> list[tuple[KernelHost, str, dict[str, Any]]]:
        """The widget messages sent (a join also asks for a snapshot)."""
        return [one for one in self.sent if one[1] == "comm"]


@dataclass
class Rig:
    fw: FileWorld
    service: NotebookService
    transport: _Transport

    @property
    def channel(self) -> str:
        return nb_channel(self.fw.node_id)


@pytest.fixture
async def rig(real_session: AsyncSession, org_admin: OrgWithAdmin) -> Rig:
    fw = await file_world(real_session, org_admin, content=NOTEBOOK, name="analysis.alknb.py")
    transport = _Transport()
    service = NotebookService(
        transport=transport,
        feed=NotebookFeed(EventHub()),
        carets=CaretBoard(),
        decide=decide_on_record,
    )

    async def view_for(**_: Any) -> Any:
        # The join's snapshot carries the view; its shape is not under test.
        return SimpleNamespace(model_dump=lambda **__: {"token": "1.AA", "cells": []})

    service.view_for = view_for  # type: ignore[method-assign]
    return Rig(fw, service, transport)


async def _hold_folder(db: AsyncSession, fw: FileWorld, *, mount: bool = False) -> str:
    """A box takes the lease on the notebook's folder: as a chat's box (it
    takes what people write there) or as a mount (it takes nothing).
    Returns its machine id."""
    owner = fw.world.owner.user
    token, machine_id = await registered_box(
        db, user_id=owner.id, email=owner.email, org_id=fw.world.org_id
    )
    await db.commit()
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as box:
        holder = MockHolder(box, folder.drive_id, folder.id, machine=machine_id)

        def idem() -> dict[str, str]:
            return {"Idempotency-Key": uuid.uuid4().hex}

        taken = (
            await holder.take(db, idem, purpose="mount")
            if mount
            else await holder.take(db, idem, purpose="chat", inbound=True, live=True)
        )
    assert taken.status_code == 200, taken.text
    return machine_id


async def _socket(rig: Rig, person: Person, *, agent_id: str | None = None) -> SocketSession:
    org_id = rig.fw.world.org_id
    _, claims = encode_session_token(
        user_id=person.user.id, email=person.user.email, org_team_id=org_id, platform_role=None
    )
    runtime = SimpleNamespace(crdt=_Lane(), notebooks=rig.service)
    socket = SocketSession(
        websocket=cast(WebSocket, _End()),
        user=person.user,
        claims=cast(SessionClaims, claims),
        peer_id="p-nb",
        runtime=cast(RealtimeRuntime, runtime),
        registry=cast(DocRegistry, None),
        ref=EntitlementRef(person.ent),
        agent_id=agent_id,
    )
    end = cast(_End, socket.websocket)
    end.inbox = [json.dumps({"t": "subscribe", "channel": rig.channel})]
    await socket._inbound()
    assert socket.notebooks is not None
    await socket.notebooks.flush()
    assert end.sent[0]["t"] == "subscribed", end.sent
    end.sent.clear()
    return socket


async def _send(socket: SocketSession, rig: Rig, frame_id: str) -> list[dict[str, Any]]:
    """Send one widget message through the socket's inbound loop; what the
    socket answered."""
    end = cast(_End, socket.websocket)
    end.inbox = [
        json.dumps(
            {
                "t": "nb.comm",
                "channel": rig.channel,
                "frame_id": frame_id,
                "comm_id": "w1",
                "msg_id": "m1",
                "content": {"method": "update", "state": {"value": 3}},
            }
        )
    ]
    await socket._inbound()
    assert socket.notebooks is not None
    await socket.notebooks.flush()
    return end.sent


async def _decisions(org_id: uuid.UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == "authz.decision")
            .order_by(EventOutbox.id)
        )
        return [row for row in rows.scalars() if row.payload.get("policy") == "notebook.run"]


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(row.payload["effect"], row.payload["reason"]) for row in rows]


def _mine(rig: Rig, person: Person) -> str:
    return frames.mint(owner=person.user.id, item_id=rig.fw.node_id, peer_id="p-nb")


@pytest.mark.parametrize(
    ("who", "decided"),
    [
        pytest.param("commenter", [("deny", "needs_can_edit")], id="can-comment"),
        pytest.param("reader", [("deny", "needs_can_edit")], id="can-view"),
    ],
)
async def test_a_socket_comm_from_someone_who_may_not_run_is_refused_on_the_record(
    real_session: AsyncSession, rig: Rig, who: str, decided: list[tuple[str, str]]
) -> None:
    await _hold_folder(real_session, rig.fw)
    person = getattr(rig.fw.world, who)
    socket = await _socket(rig, person)
    answered = await _send(socket, rig, _mine(rig, person))
    assert [(f["t"], f["code"]) for f in answered] == [("error", "forbidden")]
    assert answered[0]["message"] == "Running a notebook takes Can edit"
    (row,) = await _decisions(rig.fw.world.org_id)
    assert _effects([row]) == decided
    assert (row.payload["action"], row.payload["method"]) == ("run", SOCKET_METHOD)
    assert row.payload["path"] == rig.channel
    assert row.payload["resource"]["id"] == str(rig.fw.node_id)
    assert rig.transport.comms() == []
    # Refused, not dropped: the person still reads the notebook.
    assert socket.notebooks is not None and socket.notebooks.holds(rig.channel)


async def test_a_socket_comm_from_a_writer_reaches_the_box_on_the_record(
    real_session: AsyncSession, rig: Rig
) -> None:
    machine_id = await _hold_folder(real_session, rig.fw)
    writer = rig.fw.world.writer
    socket = await _socket(rig, writer)
    frame_id = _mine(rig, writer)
    answered = await _send(socket, rig, frame_id)
    assert answered == []
    (row,) = await _decisions(rig.fw.world.org_id)
    assert _effects([row]) == [("allow", "can_edit")]
    assert row.payload["attrs"] == {
        "rung": "writer",
        "scope_held": True,
        "scope_rung": "writer",
        "lease_admits_writes": True,
    }
    ((host, op, body),) = rig.transport.comms()
    assert (host.machine_id, host.item_id, op) == (machine_id, rig.fw.node_id, "comm")
    assert body == {
        "frame_id": frame_id,
        "comm_id": "w1",
        "msg_id": "m1",
        "content": {"method": "update", "state": {"value": 3}},
        "buffers": [],
        "requested_by": {
            "kind": "person",
            "id": f"user:{writer.user.id}",
            "display_name": writer.user.display_name,
        },
    }


@pytest.mark.parametrize(
    "whose",
    [
        pytest.param("another-person-s", id="another-person-s-frame"),
        pytest.param("another-notebook-s", id="a-frame-of-another-notebook"),
        pytest.param("forged", id="a-forged-frame"),
    ],
)
async def test_a_socket_comm_through_a_frame_that_is_not_the_sender_s_is_refused(
    real_session: AsyncSession, rig: Rig, whose: str
) -> None:
    """The writer may run; the frame decides. The allow rides the refused
    message's transaction and is rolled back with it, as the route's is."""
    await _hold_folder(real_session, rig.fw)
    writer = rig.fw.world.writer
    owner_frame = frames.mint(
        owner=rig.fw.world.owner.user.id, item_id=rig.fw.node_id, peer_id=None
    )
    forged = owner_frame.split(".")
    forged[0] = writer.user.id.hex
    frame_id = {
        "another-person-s": owner_frame,
        "another-notebook-s": frames.mint(owner=writer.user.id, item_id=uuid.uuid4(), peer_id=None),
        "forged": ".".join(forged),
    }[whose]
    socket = await _socket(rig, writer)
    answered = await _send(socket, rig, frame_id)
    assert [(f["t"], f["code"]) for f in answered] == [("error", "not_found")]
    assert await _decisions(rig.fw.world.org_id) == []
    assert rig.transport.comms() == []
    assert socket.notebooks is not None and socket.notebooks.holds(rig.channel)


async def test_a_socket_comm_is_refused_while_the_lease_refuses_writes(
    real_session: AsyncSession, rig: Rig
) -> None:
    await _hold_folder(real_session, rig.fw, mount=True)
    writer = rig.fw.world.writer
    socket = await _socket(rig, writer)
    answered = await _send(socket, rig, _mine(rig, writer))
    assert [(f["t"], f["code"]) for f in answered] == [("error", "forbidden")]
    assert _effects(await _decisions(rig.fw.world.org_id)) == [("deny", "lease_refuses_writes")]
    assert rig.transport.comms() == []


async def test_an_agent_socket_is_decided_as_the_agent_not_as_its_person(
    real_session: AsyncSession, rig: Rig
) -> None:
    """The writer's own socket would be carried (above); the same writer's
    socket opened as an agent the drive cannot place on this chat's folder is
    the agent, and is answered as for a notebook it cannot reach."""
    await _hold_folder(real_session, rig.fw)
    writer = rig.fw.world.writer
    socket = await _socket(rig, writer, agent_id=str(uuid.uuid4()))
    answered = await _send(socket, rig, _mine(rig, writer))
    assert [(f["t"], f["code"]) for f in answered] == [("error", "not_found")]
    assert await _decisions(rig.fw.world.org_id) == []
    assert rig.transport.comms() == []


async def test_a_socket_comm_on_a_notebook_the_sender_lost_drops_the_channel(
    real_session: AsyncSession, rig: Rig
) -> None:
    """Someone the notebook is not shared with is answered as for a missing
    notebook, before any run decision, and the channel goes."""
    await _hold_folder(real_session, rig.fw)
    stranger = rig.fw.world.stranger
    socket = await _socket(rig, stranger)
    answered = await _send(socket, rig, _mine(rig, stranger))
    assert [(f["t"], f["code"]) for f in answered] == [("error", "not_found")]
    assert await _decisions(rig.fw.world.org_id) == []
    assert rig.transport.comms() == []
    assert socket.notebooks is not None and not socket.notebooks.holds(rig.channel)
