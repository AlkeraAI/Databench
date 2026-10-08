"""The Loro CRDT lane over real sockets on a real server.

Browser tabs are played by Loro documents in this process; the server under
test runs its sandbox workers as real subprocesses. Every assertion is on the
frames a tab receives and on what the database holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import pytest
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import CrdtDoc, User, WorkspaceObject
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_PROTOCOL,
    CrdtChunk,
    decode_b64,
    encode_b64,
)
from backend.services.crdt.chunks import ChunkAssembler, split
from loro import EphemeralStore, Side
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.crdt.crdt_world import Peer, Person, World, kill_hard, make_world, same_vv
from tests.test_ws_gateway import Socket, connect, logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


class Tab:
    """One browser tab on the workspace channel: its socket and its Loro copy."""

    def __init__(self, sock: Socket, channel: str, *, container: str = "draft") -> None:
        self.sock = sock
        self.channel = channel
        self.container = container
        self.peer: Peer | None = None
        self.epoch = 0
        self.inbox = ChunkAssembler(max_total_bytes=8 * 1024 * 1024)

    def envelope(
        self, kind: str, payload: dict[str, Any], *, epoch: int | None = None
    ) -> dict[str, Any]:
        return self.sock.envelope(
            self.channel, kind, epoch=self.epoch if epoch is None else epoch, payload=payload
        )

    async def hello(self, *, loro_peer: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"proto": CRDT_PROTOCOL, "loro": "1.16.4", "doc_schema": 1}
        if self.peer is not None:
            payload |= {"vv_b64": encode_b64(self.peer.vv), "epoch_seen": self.epoch}
            payload["loro_peer"] = loro_peer if loro_peer is not None else self.peer.peer
        await self.sock.send(self.envelope("hello", payload, epoch=self.epoch))
        sync = await self._snapshot()
        body = sync["payload"]
        if self.peer is None or self.peer.peer != body["loro_peer"]:
            fresh = Peer(body["loro_peer"], container=self.container)
            if self.peer is not None:
                fresh.receive(self.peer.since(b"\x00"))
            self.peer = fresh
        self.peer.receive(self._data(body))
        self.epoch = sync["epoch"]
        return sync

    async def _snapshot(self) -> dict[str, Any]:
        while True:
            frame = await self.sock.recv_until(
                lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("snapshot", "error")
            )
            envelope = frame["envelope"]
            assert envelope["kind"] == "snapshot", envelope
            if envelope["payload"].get("chunk") is None:
                return dict(envelope)
            chunk = CrdtChunk.model_validate(envelope["payload"]["chunk"])
            whole = self.inbox.add(chunk)
            if whole is not None:
                envelope["payload"] = {
                    **envelope["payload"],
                    "data_b64": encode_b64(whole),
                    "chunk": None,
                }
                return dict(envelope)

    def _data(self, body: dict[str, Any]) -> bytes:
        return decode_b64(body["data_b64"])

    async def send_update(
        self, data: bytes, *, update_id: str | None = None, epoch: int | None = None
    ) -> dict[str, Any]:
        update_id = update_id or f"u-{uuid.uuid4().hex[:8]}"
        if len(data) <= CRDT_CHUNK_BYTES:
            await self.sock.send(
                self.envelope(
                    "crdt",
                    {"t": "update", "update_id": update_id, "data_b64": encode_b64(data)},
                    epoch=epoch,
                )
            )
        else:
            for piece in split(data, update_id):
                await self.sock.send(
                    self.envelope(
                        "crdt",
                        {
                            "t": "update",
                            "update_id": update_id,
                            "chunk": piece.model_dump(mode="json"),
                        },
                        epoch=epoch,
                    )
                )
        frame = await self.sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error")
        )
        return dict(frame["envelope"])

    async def type(self, at: int, text: str) -> dict[str, Any]:
        assert self.peer is not None
        return await self.send_update(self.peer.type(at, text))

    async def receive_update(self) -> dict[str, Any]:
        """The next update another tab wrote, reassembled and imported."""
        while True:
            frame = await self.sock.recv_until(
                lambda f: (
                    f["t"] == "doc"
                    and f["envelope"]["kind"] == "crdt"
                    and f["envelope"]["payload"].get("t") == "update"
                )
            )
            payload = frame["envelope"]["payload"]
            if payload.get("chunk"):
                whole = self.inbox.add(CrdtChunk.model_validate(payload["chunk"]))
                if whole is None:
                    continue
                data = whole
            else:
                data = decode_b64(payload["data_b64"])
            assert self.peer is not None
            self.peer.receive(data)
            return dict(frame["envelope"])


@asynccontextmanager
async def _tabs(server: str, world: World, *people: Person) -> Any:
    async with AsyncExitStack() as stack:
        tabs = []
        for person in people:
            client = await logged_in(person.user.email, person.password)
            stack.push_async_callback(client.aclose)
            sock = await stack.enter_async_context(connect(server, client))
            tabs.append(Tab(sock, world.ref.channel))
        yield tabs


async def test_two_people_type_into_one_draft_and_see_each_other(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        assert await a.sock.subscribe(world.ref.channel) is True
        assert await b.sock.subscribe(world.ref.channel) is True
        sync = await a.hello()
        assert sync["payload"]["mode"] == "snapshot"
        assert sync["payload"]["loro_peer"] > 1023
        assert sync["payload"]["limits"]["chunk_bytes"] == CRDT_CHUNK_BYTES
        await b.hello()
        assert a.peer is not None and b.peer is not None and a.peer.peer != b.peer.peer

        ack = await a.type(0, "hello")
        assert (ack["kind"], ack["payload"]["changed"]) == ("ack", True)
        assert same_vv(decode_b64(ack["payload"]["vv_b64"]), a.peer.vv)
        seen = await b.receive_update()
        assert seen["payload"]["user_id"] == str(world.owner.user.id)
        assert seen["payload"]["loro_peer"] == a.peer.peer
        assert b.peer.text == "hello"

        await b.type(5, " world")
        await a.receive_update()
        assert a.peer.text == b.peer.text == "hello world"
        # Neither tab hears its own update back: it was answered with an ack.
        await a.sock.expect_nothing(0.5)
    row = (
        await real_session.execute(select(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id))
    ).scalar_one()
    assert row.projection["text"] == "hello world"


@pytest.mark.parametrize(
    ("who", "can_write", "code"),
    [
        pytest.param("reader", False, None, id="can-view"),
        pytest.param("commenter", False, None, id="can-comment"),
        pytest.param("stranger", None, "not_found", id="not-shared"),
    ],
)
async def test_what_each_rung_is_told_at_subscribe(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    who: str,
    can_write: bool | None,
    code: str | None,
) -> None:
    world = await make_world(real_session, org_admin)
    person: Person = getattr(world, who)
    async with _tabs(uvicorn_server, world, person) as (tab,):
        if code is not None:
            assert (await tab.sock.subscribe_error(world.ref.channel))["code"] == code
            return
        assert await tab.sock.subscribe(world.ref.channel) is can_write
        await tab.hello()
        refused = await tab.type(0, "x")
        assert (refused["kind"], refused["payload"]["code"]) == ("error", "forbidden")
        assert refused["payload"]["update_id"]


async def test_an_org_with_no_settings_of_its_own_is_served_the_lane(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Subscribe, hello and an update all answer an org whose settings row was
    never written: no per-org switch stands between a chat and its live draft."""
    from alkera_core.models import OrgSettings
    from sqlalchemy import delete

    world = await make_world(real_session, org_admin)
    await real_session.execute(delete(OrgSettings).where(OrgSettings.org_team_id == world.org_id))
    await real_session.commit()
    async with _tabs(uvicorn_server, world, world.owner) as (tab,):
        assert await tab.sock.subscribe(world.ref.channel) is True
        await tab.hello()
        assert (await tab.type(0, "typed")).get("kind") == "ack"


async def test_an_update_before_hello_or_in_another_epoch_is_refused_in_band(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner) as (tab,):
        await tab.sock.subscribe(world.ref.channel)
        tab.epoch = 1
        early = await tab.send_update(b"", update_id="u-early")
        assert (early["payload"]["code"], early["payload"]["update_id"]) == (
            "not_synced",
            "u-early",
        )
        await tab.hello()
        assert tab.peer is not None
        stale = await tab.send_update(tab.peer.type(0, "x"), epoch=2)
        assert (stale["payload"]["code"], stale["epoch"]) == ("stale_epoch", 1)
        reload = await tab.sock.next_doc("reload")
        assert (reload["epoch"], reload["payload"]["reason"]) == (1, "stale_epoch")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        pytest.param(
            {"proto": 99, "loro": "9", "doc_schema": 1}, "crdt_unsupported", id="newer-protocol"
        ),
        pytest.param({"loro": "1"}, "bad_hello", id="not-a-crdt-hello"),
    ],
)
async def test_a_hello_the_server_cannot_serve_is_refused(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    payload: dict[str, Any],
    code: str,
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner) as (tab,):
        await tab.sock.subscribe(world.ref.channel)
        await tab.sock.send(tab.envelope("hello", payload, epoch=0))
        assert (await tab.sock.next_doc("error"))["payload"]["code"] == code


async def test_a_large_update_travels_in_pieces_both_ways(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        for tab in (a, b):
            await tab.sock.subscribe(world.ref.channel)
            await tab.hello()
        assert a.peer is not None and b.peer is not None
        # A paste that is then mostly cut: the update carries every character
        # typed, the draft keeps few of them.
        before = a.peer.doc.oplog_vv
        text = a.peer.doc.get_text("draft")
        text.insert(0, "x" * 40_000)
        text.delete(1_000, 39_000)
        a.peer.doc.commit()
        from loro import ExportMode

        big = bytes(a.peer.doc.export(ExportMode.Updates(before)))
        assert len(big) > CRDT_CHUNK_BYTES
        ack = await a.send_update(big)
        assert ack["kind"] == "ack", ack
        await b.receive_update()
        assert b.peer.text == a.peer.text == "x" * 1_000


async def test_a_reconnecting_tab_keeps_its_peer_and_a_second_tab_gets_its_own(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner) as (first,):
        await first.sock.subscribe(world.ref.channel)
        await first.hello()
        assert first.peer is not None
        await first.type(0, "abc")
        mine = first.peer.peer
        # The same socket saying hello again with its vector keeps its peer.
        again = await first.hello()
        assert again["payload"]["loro_peer"] == mine
        assert again["payload"]["mode"] == "updates"
        # Another socket offering the same peer while this one holds it does not get it.
        async with _tabs(uvicorn_server, world, world.owner) as (second,):
            await second.sock.subscribe(world.ref.channel)
            second.peer, second.epoch = first.peer, first.epoch
            taken = await second.hello(loro_peer=mine)
            assert taken["payload"]["loro_peer"] != mine
            # ...and writing under its own peer works on the copy it carried over.
            assert (await second.type(0, ">")).get("kind") == "ack"


async def test_a_tab_coming_back_to_a_new_epoch_writes_as_a_new_peer(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Its counters start again in the new epoch's document; under its old
    peer they would write operation ids that peer already spent."""
    from alkera_core.db.session import AsyncSessionLocal
    from backend.app_factory import process_app

    fastapi_app = process_app()
    from backend.services.realtime.runtime import runtime_of

    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner) as (tab,):
        await tab.sock.subscribe(world.ref.channel)
        await tab.hello()
        await tab.type(0, "abc")
        assert tab.peer is not None
        mine = tab.peer.peer
        runtime = runtime_of(fastapi_app)
        assert runtime is not None and runtime.crdt is not None
        async with AsyncSessionLocal() as db:
            await runtime.crdt.restart(db, world.ref, reason="test", quarantine=False)
            await db.commit()
        await tab.sock.send(
            tab.envelope(
                "hello",
                {
                    "proto": CRDT_PROTOCOL,
                    "loro": "1.16.4",
                    "doc_schema": 1,
                    "vv_b64": encode_b64(tab.peer.vv),
                    "epoch_seen": 1,
                    "loro_peer": mine,
                },
                epoch=1,
            )
        )
        sync = await tab._snapshot()
        assert sync["epoch"] == 2
        assert sync["payload"]["mode"] == "snapshot"
        assert sync["payload"]["loro_peer"] != mine


async def test_a_caret_reaches_the_other_tab_stamped_with_who_moved_it(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        for tab in (a, b):
            await tab.sock.subscribe(world.ref.channel)
            await tab.hello()
        assert a.peer is not None
        await a.type(0, "hello")
        await b.receive_update()
        cursor = a.peer.doc.get_text("draft").get_cursor(3, Side.Middle)
        assert cursor is not None
        store = EphemeralStore(60_000)
        store.set(str(a.peer.peer), {"anchor": cursor.encode(), "focus": cursor.encode()})
        await a.sock.send(
            a.envelope(
                "crdt", {"t": "ephemeral", "data_b64": encode_b64(bytes(store.encode_all()))}
            )
        )
        frame = await b.sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["payload"].get("t") == "ephemeral"
        )
        caret = frame["envelope"]["payload"]
        assert caret["user_id"] == str(world.owner.user.id)
        assert caret["loro_peer"] == a.peer.peer
        assert caret["email"] == world.owner.user.email
        relayed = EphemeralStore(60_000)
        relayed.apply(decode_b64(caret["data_b64"]))
        assert set(relayed.get_all_states()) == {str(a.peer.peer)}
        # A caret claiming someone else's key is refused to its sender, relayed to nobody.
        forged = EphemeralStore(60_000)
        forged.set("99999", {"anchor": cursor.encode()})
        await a.sock.send(
            a.envelope(
                "crdt", {"t": "ephemeral", "data_b64": encode_b64(bytes(forged.encode_all()))}
            )
        )
        assert (await a.sock.next_doc("error"))["payload"]["reason"] == "ephemeral"
        await b.sock.expect_nothing(0.5)


async def test_a_colleague_asserting_the_bound_machines_id_reads_no_draft(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat's machine id is on the chat; a colleague can put it on their own
    socket's agent headers. What decides the lane's access is the machine the
    socket was VERIFIED as, never that bare assertion, so a colleague the chat
    was never shared with is refused its draft exactly as without it."""
    from alkera_core.authz import agent_headers
    from backend.services.chats import chat_service

    world = await make_world(real_session, org_admin)
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    bound, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Bound",
        client_id=None,
        machine_id="box-bound-1",
        machine_status="ready",
    )
    await real_session.commit()
    channel = f"doc:chat_draft:{bound.id}"
    client = await logged_in(world.stranger.user.email, world.stranger.password)
    try:
        # The assertion rides the ticket: minted with the machine's id as the agent.
        minted = await client.post("/api/v1/ws/tickets", headers=agent_headers("box-bound-1"))
        assert minted.status_code == 200, minted.text
        async with connect(uvicorn_server, client, ticket=minted.json()["ticket"]) as sock:
            await sock.send({"t": "subscribe", "channel": channel})
            frame = await sock.recv_until(
                lambda f: f.get("channel") == channel and f["t"] in ("subscribed", "error")
            )
            assert frame["t"] == "error", frame
            assert frame["code"] == "not_found", frame
    finally:
        await client.aclose()


async def test_a_tab_that_leaves_takes_its_caret_with_it(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other tabs are told at once, by the server, that the peer is gone;
    nobody else may say so."""
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        for tab in (a, b):
            await tab.sock.subscribe(world.ref.channel)
            await tab.hello()
        assert a.peer is not None and b.peer is not None
        # A tab cannot announce anybody's departure, its own included.
        await a.sock.send(a.envelope("crdt", {"t": "gone", "loro_peer": b.peer.peer}))
        assert (await a.sock.next_doc("error"))["payload"]["code"] == "unsupported_kind"
        await b.sock.expect_nothing(0.5)
        leaving = b.peer.peer
        await b.sock.ws.close()
        frame = await a.sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["payload"].get("t") == "gone"
        )
        assert frame["envelope"]["payload"]["loro_peer"] == leaving


async def test_a_writer_demoted_mid_session_loses_the_composer_at_once(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.writer) as (tab,):
        assert await tab.sock.subscribe(world.ref.channel) is True
        await tab.hello()
        chat = await real_session.get(WorkspaceObject, uuid.UUID(world.ref.doc_id))
        owner = await real_session.get(User, world.owner.user.id)
        assert chat is not None and owner is not None
        await share_chat_with(
            real_session,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=world.writer.user.id),
            role=ROLE_READER,
        )
        told = await tab.sock.recv_until(lambda f: f["t"] == "subscribed")
        assert (told["channel"], told["can_write"]) == (world.ref.channel, False)
        refused = await tab.type(0, "x")
        assert refused["payload"]["code"] == "forbidden"


async def test_the_lane_survives_its_sandbox_worker_being_killed(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:

    from backend.app_factory import process_app

    fastapi_app = process_app()
    from backend.services.realtime.runtime import runtime_of

    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        for tab in (a, b):
            await tab.sock.subscribe(world.ref.channel)
            await tab.hello()
        await a.type(0, "before ")
        await b.receive_update()
        runtime = runtime_of(fastapi_app)
        assert runtime is not None and runtime.crdt is not None
        for pid in runtime.crdt.pool.pids():
            if pid is not None:
                # It may have been replaced on its own a moment ago.
                kill_hard(pid)
        await asyncio.sleep(0.2)
        ack = await a.type(7, "after")
        assert ack["kind"] == "ack", ack
        await b.receive_update()
        assert b.peer is not None and b.peer.text == "before after"


async def test_a_reader_opens_the_draft_without_minting_a_peer(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A Loro peer is something to write as. A tab that may only read is handed
    a number its client can set, but nothing is stored for it, nothing is held,
    and its leaving is nobody's news: hello after hello costs no row."""
    from alkera_core.models import CrdtPeer
    from sqlalchemy import func

    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.reader) as (owner, reader):
        assert await owner.sock.subscribe(world.ref.channel) is True
        assert await reader.sock.subscribe(world.ref.channel) is False
        await owner.hello()
        await owner.type(0, "hi")
        handed: set[int] = set()
        for _ in range(3):
            reader.peer = None
            sync = await reader.hello()
            handed.add(sync["payload"]["loro_peer"])
            assert reader.peer is not None and reader.peer.text == "hi"
        assert all(peer > 1023 for peer in handed)
        stored = await real_session.scalar(
            select(func.count())
            .select_from(CrdtPeer)
            .where(
                CrdtPeer.doc_id == world.ref.doc_id,
                CrdtPeer.user_id == world.reader.user.id,
            )
        )
        assert stored == 0
        assert owner.peer is not None and owner.peer.peer not in handed
        await reader.sock.ws.close()
        await owner.sock.expect_nothing(0.7)


async def test_a_reader_granted_writing_says_hello_again_before_it_writes(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The id a reader was handed is not a stored peer: an update under it is
    refused until the hello that claims a real one, and that one is written."""
    from alkera_core.files.authz.ladder import ROLE_WRITER
    from alkera_core.models import CrdtPeer

    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.reader) as (tab,):
        assert await tab.sock.subscribe(world.ref.channel) is False
        await tab.hello()
        assert tab.peer is not None
        as_reader = tab.peer.peer
        chat = await real_session.get(WorkspaceObject, uuid.UUID(world.ref.doc_id))
        owner = await real_session.get(User, world.owner.user.id)
        assert chat is not None and owner is not None
        await share_chat_with(
            real_session,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=world.reader.user.id),
            role=ROLE_WRITER,
        )
        told = await tab.sock.recv_until(lambda f: f["t"] == "subscribed")
        assert told["can_write"] is True
        refused = await tab.type(0, "x")
        assert refused["payload"]["code"] == "not_synced"
        tab.peer = None
        sync = await tab.hello()
        assert sync["payload"]["loro_peer"] != as_reader
        assert (await tab.type(0, "now")).get("kind") == "ack"
        stored = (
            await real_session.execute(
                select(CrdtPeer.loro_peer).where(CrdtPeer.user_id == world.reader.user.id)
            )
        ).scalars()
        assert list(stored) == [sync["payload"]["loro_peer"]]


async def test_a_saturated_lane_makes_requests_wait_without_touching_the_database(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A request that would wait for a sandbox slot while holding a pooled
    connection and the document's row lock waits at the door instead: with
    every admission taken, a hello and an update stay unanswered and write
    nothing (not even the document's first row), and each goes through, never
    refused, once admission frees."""
    from alkera_core.models import CrdtUpdate
    from backend.app_factory import process_app

    fastapi_app = process_app()
    from backend.services.realtime.runtime import runtime_of
    from sqlalchemy import func

    world = await make_world(real_session, org_admin)
    runtime = runtime_of(fastapi_app)
    assert runtime is not None and runtime.crdt is not None
    docs = runtime.crdt

    async def rows() -> tuple[int, int]:
        real_session.expire_all()
        doc = await real_session.scalar(
            select(func.count()).select_from(CrdtDoc).where(CrdtDoc.doc_id == world.ref.doc_id)
        )
        log = await real_session.scalar(
            select(func.count())
            .select_from(CrdtUpdate)
            .where(CrdtUpdate.doc_id == world.ref.doc_id)
        )
        await real_session.commit()
        return int(doc or 0), int(log or 0)

    async with _tabs(uvicorn_server, world, world.owner) as (tab,):
        await tab.sock.subscribe(world.ref.channel)
        async with AsyncExitStack() as full:
            for _ in range(docs.admission_slots):
                await full.enter_async_context(docs.admitted())
            hello = asyncio.create_task(tab.hello())
            await asyncio.sleep(0.5)
            assert not hello.done(), "a hello waits for admission rather than being refused"
            assert await rows() == (0, 0)
        await asyncio.wait_for(hello, 10)
        assert await rows() == (1, 0)

        assert tab.peer is not None
        async with AsyncExitStack() as full:
            for _ in range(docs.admission_slots):
                await full.enter_async_context(docs.admitted())
            update = tab.peer.type(0, "held")
            sent = asyncio.create_task(tab.send_update(update, update_id="u-wait"))
            await asyncio.sleep(0.5)
            assert not sent.done(), "an update waits for admission rather than being refused"
            assert await rows() == (1, 0)
        ack = await asyncio.wait_for(sent, 10)
        assert ack["kind"] == "ack", ack
        assert await rows() == (1, 1)


async def test_a_reload_ends_the_carets_until_the_next_hello(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Restarting the history tells every tab ``reload``. A tab that keeps
    sending carets afterwards is told to say hello again — the hello that
    re-checks its access and hands it the new epoch — and nothing it sends
    reaches anyone."""
    from alkera_core.db.session import AsyncSessionLocal
    from backend.app_factory import process_app

    fastapi_app = process_app()
    from backend.services.realtime.runtime import runtime_of

    world = await make_world(real_session, org_admin)
    async with _tabs(uvicorn_server, world, world.owner, world.writer) as (a, b):
        for tab in (a, b):
            await tab.sock.subscribe(world.ref.channel)
            await tab.hello()
        assert a.peer is not None
        await a.type(0, "hello")
        await b.receive_update()
        cursor = a.peer.doc.get_text("draft").get_cursor(3, Side.Middle)
        assert cursor is not None
        store = EphemeralStore(60_000)
        store.set(str(a.peer.peer), {"anchor": cursor.encode(), "focus": cursor.encode()})
        caret = a.envelope(
            "crdt", {"t": "ephemeral", "data_b64": encode_b64(bytes(store.encode_all()))}
        )

        runtime = runtime_of(fastapi_app)
        assert runtime is not None and runtime.crdt is not None
        async with AsyncSessionLocal() as db:
            await runtime.crdt.restart(db, world.ref, reason="compacted", quarantine=False)
            await db.commit()
        for tab in (a, b):
            reload = await tab.sock.next_doc("reload")
            assert reload["payload"]["reason"] == "compacted"

        await a.sock.send(caret)
        refused = await a.sock.next_doc("error")
        assert refused["payload"]["code"] == "not_synced"
        # The other tab may hear that the caret went; it never hears a new one.
        heard: list[dict[str, Any]] = []
        with contextlib.suppress(TimeoutError):
            while True:
                heard.append(await b.sock.recv(0.7))
        carets = [
            f for f in heard if f["t"] == "doc" and f["envelope"]["payload"].get("t") == "ephemeral"
        ]
        assert carets == []
