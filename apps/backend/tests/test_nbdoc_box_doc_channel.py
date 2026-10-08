"""A box follows a notebook's live document on its own machine socket.

The box's notebook store subscribes to ``doc:notebook:<item_id>`` on the
socket its org worker credential opens, and reads the notebook back whenever
the channel says the document moved. The machine holding the notebook's
folder (the one its kernel runs on) is granted that channel, read-only; any
other machine, the same machine on another org's credential, and a holder
that handed its lease back are told ``not_found``, the answer a stranger gets.

Real Postgres, real Files leases, the real CRDT lane and a real server; the
box side is the follower the box itself runs (``RealtimeDocSignals``), so the
subscribe frame and the signal it reads are the box's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from alkera_cli.notebooks.store_loro import DocSignal, RealtimeDocSignals, StoreError
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.realtime import WS_PATH
from backend.services.files.chat_uploads import put_chat_upload
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fastapi_app, login
from tests.crdt.file_world import FileWorld, acting
from tests.crdt.test_nbdoc_real_format import FULL
from tests.crdt.test_nbdoc_session import WEEKLY, notebook_world
from tests.files._live_holder import MockHolder
from tests.test_machine_principal_routes import Box, _beat, _box, _chat, _org
from tests.test_machine_worker_credential import _worker
from tests.test_ws_gateway import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

#: How long a signal that should arrive is waited for.
SIGNAL_SECONDS = 20.0


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


def _channel(fw: FileWorld) -> str:
    return f"doc:notebook:{fw.node_id}"


async def _bind(chat_id: str, machine_id: uuid.UUID) -> None:
    """Bind the chat to the box the way placement does: its spec names it."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
            ),
            {"id": uuid.UUID(chat_id), "machine": str(machine_id)},
        )
        await session.commit()


def _bearer_client(token: str, base_url: str = "http://test") -> AsyncClient:
    if base_url == "http://test":
        return AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
        )
    return AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {token}"})


@dataclass
class World:
    fw: FileWorld
    box: Box
    #: The box's org worker credential for the notebook's org.
    worker: str
    holder: MockHolder


async def _take_folder(db: AsyncSession, fw: FileWorld, worker: AsyncClient) -> MockHolder:
    """The box takes the chat folder holding the notebook, as a chat's box
    does: taking what people write there, live."""
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    holder = MockHolder(worker, folder.drive_id, folder.id)
    taken = await holder.take(db, _idem, purpose="chat", inbound=True, live=True)
    assert taken.status_code == 200, taken.text
    return holder


@pytest.fixture
async def world(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> AsyncIterator[World]:
    """A notebook in a chat's folder, and a pool box holding that folder on
    its worker credential for the notebook's org."""
    fw = await notebook_world(real_session, org_admin, FULL)
    box = await _box(org_admin, tenancy="pool")
    await _beat(client, box)
    await _bind(fw.world.ref.doc_id, box.machine_id)
    worker = await _worker(client, box, fw.world.org_id)
    async with _bearer_client(worker) as worker_client:
        holder = await _take_folder(real_session, fw, worker_client)
        yield World(fw, box, worker, holder)


@contextlib.asynccontextmanager
async def _follow(server: str, token: str, item_id: uuid.UUID) -> AsyncIterator[_Follower]:
    """The box's own follower, on the served backend, under ``token``."""
    async with _bearer_client(token, f"http://{server}") as http:
        signals = RealtimeDocSignals(http=http, ws_url=f"ws://{server}{WS_PATH}")
        follower = _Follower(signals.watch(str(item_id)))
        try:
            yield follower
        finally:
            await follower.stream.aclose()


@dataclass
class _Follower:
    stream: AsyncIterator[DocSignal]

    async def next(self, seconds: float = SIGNAL_SECONDS) -> DocSignal:
        return await asyncio.wait_for(anext(self.stream), timeout=seconds)


async def _edit(client: AsyncClient, fw: FileWorld, source: str) -> None:
    owner = fw.world.owner
    await login(client, owner.user.email, owner.password)
    node = await _drive_of(fw)
    answer = await client.post(
        f"/api/v1/notebooks/{node}/{fw.node_id}/ops",
        json={
            "ops": [{"op": "replace", "cell_id": WEEKLY, "source": source}],
            "submit_id": f"follow-{secrets.token_hex(6)}",
        },
    )
    assert answer.status_code == 200, answer.text


async def _drive_of(fw: FileWorld) -> uuid.UUID:
    async with AsyncSessionLocal() as session:
        node = await session.get(FileNode, fw.node_id)
        assert node is not None
        return uuid.UUID(str(node.drive_id))


async def test_the_box_holding_the_folder_follows_the_notebook_and_hears_a_person_s_edit(
    world: World, uvicorn_server: str, client: AsyncClient
) -> None:
    """The follower's subscribe is granted, and a person's committed edit
    reaches it as an update signal naming who made it."""
    async with _follow(uvicorn_server, world.worker, world.fw.node_id) as follower:
        assert (await follower.next()).kind == "ready"
        await _edit(client, world.fw, "weekly = 2")
        signal = await follower.next()
    assert signal.kind == "update"
    assert signal.actor_id == str(world.fw.world.owner.user.id)


async def test_the_box_s_grant_on_the_notebook_reads_and_its_frames_are_refused(
    world: World, uvicorn_server: str
) -> None:
    """The box writes a notebook through its ops route under its lease's
    fence, never on this socket: the grant says it may not write, and a frame
    it sends anyway is refused without touching the document."""
    channel = _channel(world.fw)
    async with (
        _bearer_client(world.worker, f"http://{uvicorn_server}") as http,
        connect(uvicorn_server, http) as sock,
    ):
        await sock.send({"t": "subscribe", "channel": channel})
        subscribed = await sock.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert (subscribed["t"], subscribed["channel"], subscribed["can_write"]) == (
            "subscribed",
            channel,
            False,
        )
        await sock.send(sock.envelope(channel, "hello", epoch=0, payload={}))
        refused = await sock.next_doc("error")
    assert refused["payload"]["code"] == "read_only"


async def test_a_box_that_does_not_hold_the_folder_is_told_not_found(
    world: World, uvicorn_server: str, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """Another box serving the same org, with a chat of its own there, holds
    nothing the notebook is in: its subscribe is the stranger's not-found."""
    other = await _box(org_admin, tenancy="pool")
    await _beat(client, other)
    await _chat(world.fw.world.owner.user, machine_id=other.machine_id)
    token = await _worker(client, other, world.fw.world.org_id)
    async with _follow(uvicorn_server, token, world.fw.node_id) as follower:
        with pytest.raises(StoreError) as refused:
            await follower.next()
    assert refused.value.code == "not_found"


async def test_the_holding_machine_on_another_org_s_credential_is_told_not_found(
    world: World, uvicorn_server: str, client: AsyncClient
) -> None:
    """The very machine holding the folder, speaking for another org it
    serves, may not follow this org's notebook: the credential's org bounds
    it, whatever the machine holds elsewhere."""
    _org_b, member_b = await _org()
    await _chat(member_b, machine_id=world.box.machine_id)
    token = await _worker(client, world.box, member_b.home_org_team_id)
    async with _follow(uvicorn_server, token, world.fw.node_id) as follower:
        with pytest.raises(StoreError) as refused:
            await follower.next()
    assert refused.value.code == "not_found"


async def test_a_file_that_is_not_a_notebook_is_not_followed_as_one(
    world: World, uvicorn_server: str, real_session: AsyncSession
) -> None:
    """The channel names a notebook: the holder's other files in the same
    folder are not served on it."""
    landed = await put_chat_upload(
        real_session,
        ctx=acting(world.fw.world.owner),
        chat_id=uuid.UUID(world.fw.world.ref.doc_id),
        filename="notes.py",
        content=b"x = 1\n",
    )
    await real_session.commit()
    async with _follow(uvicorn_server, world.worker, landed.node_id) as follower:
        with pytest.raises(StoreError) as refused:
            await follower.next()
    assert refused.value.code == "not_found"


async def test_a_box_that_hands_its_lease_back_stops_following_on_the_next_tick(
    world: World,
    uvicorn_server: str,
    real_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant is decided again on every keepalive tick: once the box no
    longer holds the folder, the channel is dropped with the same not-found a
    fresh subscribe now answers, and a later edit does not reach it."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 0.5)
    channel = _channel(world.fw)
    async with (
        _bearer_client(world.worker, f"http://{uvicorn_server}") as http,
        connect(uvicorn_server, http) as sock,
    ):
        await sock.send({"t": "subscribe", "channel": channel})
        subscribed = await sock.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert subscribed["t"] == "subscribed", subscribed
        released = await world.holder.release(real_session, _idem)
        assert released.status_code == 200, released.text
        dropped = await sock.recv_until(lambda f: f["t"] == "error", SIGNAL_SECONDS)
        assert (dropped["code"], dropped["channel"]) == ("not_found", channel)
        await _edit(client, world.fw, "weekly = 3")
        await sock.send({"t": "subscribe", "channel": channel})
        again = await sock.recv_until(lambda f: f["t"] in ("subscribed", "error", "doc"))
    assert (again["t"], again.get("code")) == ("error", "not_found")
