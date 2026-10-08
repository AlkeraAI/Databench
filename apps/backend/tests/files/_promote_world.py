"""A folder held live by a machine whose socket is open: the world the
bytes-on-demand tests read files in.

Everything is the real thing: the lease is taken, the tree reported and the
bytes pushed through the routes a holder calls (:class:`MockHolder`), the box
is a registered workspace machine holding ``machine:<its id>`` over a real
socket on a served app, and a reader's request parks on that app's hub. The
lease names the box's machine id, which is what routes a promote to its socket.

Spelled once here because two suites build it: the content routes' promotion
cases and the rate-limit case for the per-folder cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import websockets
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.events import EventHub, HubEvent, Subscription
from alkera_core.schemas.realtime import WS_PATH, WS_SUBPROTOCOL, WS_TICKET_SUBPROTOCOL_PREFIX
from backend.services.realtime.runtime import runtime_of
from blake3 import blake3
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client
from tests.files._boxes import registered_box
from tests.files._live_holder import PREFIX, MockHolder

#: Every wait on a frame here is for a signal the server itself sends; the
#: bound is a backstop against a hang, never the claim.
FRAME_BACKSTOP = 30.0
MTIME_NS = 1_758_625_000_123_456_789


def reported(path: str, payload: bytes, *, mtime: int = MTIME_NS) -> dict[str, Any]:
    """One tree-report entry for a file whose bytes are ``payload``."""
    return {
        "op": "upsert",
        "path": path,
        "kind": "file",
        "size": len(payload),
        "mtime_ns": mtime,
        "hash": "b3:" + blake3(payload).hexdigest(),
    }


@dataclass
class Box:
    """The machine's end of the socket."""

    ws: Any
    machine_id: str

    async def recv(self, seconds: float = FRAME_BACKSTOP) -> dict[str, Any]:
        return dict(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=seconds)))

    async def request(self, seconds: float = FRAME_BACKSTOP) -> dict[str, Any]:
        """The next ``machine.request``; anything else before it is skipped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.recv(max(0.05, deadline - loop.time()))
            if frame.get("t") == "machine.request":
                return frame

    async def ack(self, request: dict[str, Any], outcome: str, **extra: Any) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "t": "machine.ack",
                    "request_id": request["request_id"],
                    "outcome": outcome,
                    **extra,
                }
            )
        )

    async def nothing_asked(self, seconds: float = 1.0) -> None:
        """No request reaches the socket within ``seconds``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            try:
                frame = await self.recv(max(0.05, deadline - loop.time()))
            except TimeoutError:
                return
            assert frame.get("t") != "machine.request", f"the machine was asked: {frame}"


@dataclass
class World:
    drive_id: uuid.UUID
    project_id: uuid.UUID
    holder: MockHolder
    machine_id: str
    box_http: AsyncClient
    session: AsyncSession
    idem: Callable[[], dict[str, str]]

    def grants(self, node_id: uuid.UUID) -> str:
        return f"{PREFIX}/drives/{self.drive_id}/items/{node_id}/content-grants"

    def content(self, node_id: uuid.UUID) -> str:
        return f"{PREFIX}/drives/{self.drive_id}/items/{node_id}/content"

    async def node_at(self, name: bytes) -> uuid.UUID:
        row = (
            await self.session.execute(
                text(
                    "SELECT id FROM file_nodes WHERE drive_id = :drive AND name = :name "
                    "AND trashed_at IS NULL"
                ),
                {"drive": self.drive_id, "name": name},
            )
        ).scalar_one()
        await self.session.commit()
        return uuid.UUID(str(row))

    async def report(self, *entries: dict[str, Any]) -> None:
        answer = await self.holder.tree(list(entries))
        assert answer.status_code == 200, answer.text

    async def land(self, node_id: uuid.UUID, payload: bytes) -> None:
        pushed = await self.holder.push(self.session, self.idem, node_id, payload)
        await self.session.commit()
        assert pushed.status_code in (200, 201), pushed.text

    @contextlib.asynccontextmanager
    async def box(self, addr: str) -> AsyncIterator[Box]:
        """The machine's socket, holding its own channel."""
        minted = await self.box_http.post("/api/v1/ws/tickets")
        assert minted.status_code == 200, minted.text
        ticket = minted.json()["ticket"]
        async with websockets.connect(
            f"ws://{addr}{WS_PATH}",
            subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
        ) as ws:
            box = Box(ws, self.machine_id)
            assert (await box.recv())["t"] == "welcome"
            await ws.send(json.dumps({"t": "subscribe", "channel": f"machine:{self.machine_id}"}))
            subscribed = await box.recv()
            assert subscribed["t"] == "subscribed", subscribed
            yield box


async def held_world(
    files_client: AsyncClient,
    fx: Any,
    session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    *,
    org_id: uuid.UUID,
    admin_id: uuid.UUID,
    admin_email: str,
) -> World:
    """``/Shared/project`` held live, under the registered box's machine id."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await session.commit()
    project = await fx.node(b"project", kind="folder", parent=await fx.shared())
    token, machine_id = await registered_box(
        session, user_id=admin_id, email=admin_email, org_id=org_id
    )
    holder = MockHolder(files_client, drive.id, project.id, machine=machine_id)
    taken = await holder.take(session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    box_http = app_client(headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)})
    return World(
        drive_id=drive.id,
        project_id=project.id,
        holder=holder,
        machine_id=machine_id,
        box_http=box_http,
        session=session,
        idem=idem,
    )


def served_hub() -> EventHub:
    runtime = runtime_of(fastapi_app)
    assert runtime is not None, "the served app's lifespan binds the hub the reader waits on"
    return runtime.hub


@contextlib.contextmanager
def requests_heard() -> Any:
    """Every ``machine.request`` this process's hub hears while the block runs."""
    hub = served_hub()
    sub: Subscription = hub.subscribe(
        lambda event: event.type == "machine.request", label="test-requests"
    )
    try:
        yield sub
    finally:
        hub.unsubscribe(sub)


def drained(sub: Subscription) -> list[HubEvent]:
    items: list[HubEvent] = []
    while not sub.queue.empty():
        item = sub.queue.get_nowait()
        if isinstance(item, HubEvent):
            items.append(item)
    return items


__all__ = [
    "FRAME_BACKSTOP",
    "MTIME_NS",
    "Box",
    "World",
    "drained",
    "held_world",
    "reported",
    "requests_heard",
    "served_hub",
]
