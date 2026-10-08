"""Cross-instance delivery: two independent backend instances on one Postgres.

Nothing between them but the database. An event committed through instance A
reaches an event-stream client and a socket subscriber on instance B; a ticket
minted on A opens a socket on B once and is refused on replay anywhere;
presence joined on A is visible on B; and a stream or socket cut when A goes
away mid-flight resumes on B from its cursor with no gap and no duplicate.
This is the proof that the fleet needs no shared bus and no sticky sessions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import websockets
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, emit
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import User, WorkspaceObject
from alkera_core.schemas.realtime import (
    SERVER_PEER_ID,
    WS_PATH,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
)
from backend.services.chats import chat_service
from backend.services.realtime import close_codes
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import create_app
from tests.chat_declarations import declare_chat
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, make_member, serve_app, served_client
from websockets.exceptions import ConnectionClosed

STREAM = "/api/v1/events"
RECV_TIMEOUT = 8.0


@pytest.fixture(autouse=True)
def _fast_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def http_login(addr: str, email: str, password: str) -> httpx.AsyncClient:
    """A logged-in client for ``addr``; close it with ``aclosing``."""
    client = served_client(addr, timeout=httpx.Timeout(RECV_TIMEOUT))
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return client


async def mint_ticket(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/v1/ws/tickets")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["ticket"])


class Stream:
    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.aiter_lines()
        self.frames: list[dict[str, str]] = []

    async def next_frame(self, seconds: float = RECV_TIMEOUT) -> dict[str, str]:
        frame: dict[str, str] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            remaining = deadline - loop.time()
            assert remaining > 0, f"no frame within {seconds}s; partial={frame}"
            line = await asyncio.wait_for(self._lines.__anext__(), timeout=remaining)
            if line == "":
                if frame:
                    self.frames.append(frame)
                    return frame
                continue
            if line.startswith(":"):
                frame["comment"] = line[1:].strip()
                continue
            key, _, value = line.partition(":")
            frame[key] = value.strip()

    async def next_event(self, seconds: float = RECV_TIMEOUT) -> dict[str, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.next_frame(max(0.05, deadline - loop.time()))
            if "event" in frame:
                return frame

    async def next_event_named(self, name: str, seconds: float = RECV_TIMEOUT) -> dict[str, str]:
        """The next frame carrying ``event: name``, frames of other names read past.

        A stream opened after fixtures committed still receives those rows once
        the instance's listener reads them, so the frame after "connected" is
        not always the one the test just caused; on a loaded host it was a
        fixture's ``file_node.changed``. Reading to the named event asserts the
        delivery without asserting the order of the setup's rows around it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.next_event(max(0.05, deadline - loop.time()))
            if frame["event"] == name:
                return frame

    async def expect_connected(self) -> None:
        assert (await self.next_frame())["retry"] == "2000"
        assert (await self.next_frame())["comment"] == "connected"

    async def ended(self, seconds: float = RECV_TIMEOUT) -> bool:
        """Whether the server ended the stream: the body finishes or the
        connection drops. Frames that arrive first are kept."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.wait_for(self._lines.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return True
        except (httpx.RemoteProtocolError, httpx.ReadError):
            return True
        except TimeoutError:
            return False


@asynccontextmanager
async def open_stream(
    addr: str, cookies: httpx.Cookies, *, params: dict[str, str] | None = None
) -> AsyncIterator[Stream]:
    async with (
        httpx.AsyncClient(
            base_url=f"http://{addr}", cookies=cookies, timeout=httpx.Timeout(RECV_TIMEOUT)
        ) as client,
        client.stream("GET", STREAM, params=params) as response,
    ):
        assert response.status_code == 200, await response.aread()
        yield Stream(response)


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------


class Socket:
    def __init__(self, ws: Any, peer_id: str) -> None:
        self.ws = ws
        self.peer_id = peer_id

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    async def recv(self, seconds: float = RECV_TIMEOUT) -> dict[str, Any]:
        return dict(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=seconds)))

    async def recv_until(
        self, matches: Callable[[dict[str, Any]], bool], seconds: float = RECV_TIMEOUT
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.recv(max(0.05, deadline - loop.time()))
            if matches(frame):
                return frame

    async def close_code(self) -> int:
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout=RECV_TIMEOUT)
        except ConnectionClosed as exc:
            return int(exc.rcvd.code) if exc.rcvd is not None else 1006

    async def subscribe(self, channel: str) -> bool:
        await self.send({"t": "subscribe", "channel": channel})
        subscribed = await self.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert subscribed["t"] == "subscribed", subscribed
        await self.recv_until(lambda f: f["t"] == "presence" and f["event"] == "roster")
        return bool(subscribed["can_write"])

    async def subscribe_error(self, channel: str) -> dict[str, Any]:
        """The refusal, for a peer this channel does not admit."""
        await self.send({"t": "subscribe", "channel": channel})
        frame = await self.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert frame["t"] == "error", frame
        return frame

    def envelope(
        self, channel: str, kind: str, *, epoch: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        _, doc_type, doc_id = channel.split(":", 2)
        return {
            "t": "doc",
            "envelope": {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "epoch": epoch,
                "peer_id": self.peer_id,
                "seq": 0,
                "kind": kind,
                "payload": payload,
            },
        }

    async def hello(self, channel: str) -> dict[str, Any]:
        await self.send(self.envelope(channel, "hello", epoch=0, payload={}))
        frame = await self.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("snapshot", "error")
        )
        assert frame["envelope"]["kind"] == "snapshot", frame
        assert frame["envelope"]["peer_id"] == SERVER_PEER_ID
        return dict(frame["envelope"])

    async def op(self, channel: str, *, epoch: int, intent: str, **body: Any) -> dict[str, Any]:
        payload = {"op_id": f"op-{uuid4().hex[:8]}", "intent": intent, **body}
        await self.send(self.envelope(channel, "op", epoch=epoch, payload=payload))
        frame = await self.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error")
        )
        return dict(frame["envelope"])

    async def next_doc(self, kind: str) -> dict[str, Any]:
        frame = await self.recv_until(lambda f: f["t"] == "doc" and f["envelope"]["kind"] == kind)
        return dict(frame["envelope"])


@asynccontextmanager
async def connect(addr: str, ticket: str, *, expect_welcome: bool = True) -> AsyncIterator[Socket]:
    async with websockets.connect(
        f"ws://{addr}{WS_PATH}",
        subprotocols=[WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"],
    ) as ws:
        peer_id = ""
        if expect_welcome:
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT))
            assert welcome["t"] == "welcome", welcome
            peer_id = welcome["peer_id"]
        yield Socket(ws, peer_id)


async def emit_row(org_id: UUID, entity_id: str) -> int:
    async with AsyncSessionLocal() as db:
        row = await emit(
            db,
            org_id=org_id,
            type=EventType.KB_ITEM_CHANGED,
            entity="kb_item",
            entity_id=entity_id,
        )
        await db.commit()
        return int(row.id)


def chat_channel(org: OrgWithAdmin, *, owner_user_id: UUID | None = None) -> str:
    """A chat declared for ``org``, owned by its admin unless told otherwise.

    A socket cannot bring a chat into existence by asking about it, so a test
    that opens one declares it first; the document row is still created by the
    first ``hello``."""
    return declare_chat(
        org.org_id, owner_user_id=org.admin_id if owner_user_id is None else owner_user_id
    )


async def shared_chat_channel(
    db: AsyncSession,
    org: OrgWithAdmin,
    *,
    owner: User | None = None,
    viewers: tuple[User, ...] = (),
    writers: tuple[User, ...] = (),
) -> str:
    """A chat created the way ``POST /api/v1/chats`` creates it — so it has the
    Files node its ACL hangs from — shared with ``viewers`` at "Can view" and
    with ``writers`` at "Can edit", returned as a channel name.

    A chat is private to whoever made it. A second person reads one only
    through a share, and that answer is the database's, not a replica's: the
    grant is written once here and both instances resolve it from the same
    rows. A declared, node-less chat cannot carry a rung, so a test where the
    peer on instance B is somebody other than the owner has to share the real
    way — ``files.acl.grant``, through :func:`share_chat_with` — or it proves
    only that the door is shut.

    The caller needs the ``files_on`` fixture: without a configured store the
    bridge mints no node, and there is nothing to grant a rung on.
    """
    publisher = owner
    if publisher is None:
        publisher = await db.get(User, org.admin_id)
        assert publisher is not None
    chat, _ = await chat_service.create_chat(
        db,
        owner=publisher,
        org_id=publisher.home_org_team_id,
        title="Ops",
        client_id=None,
        machine_id=None,
        machine_status="none",
    )
    await db.commit()
    granted: list[tuple[Principal, str]] = [
        (Principal(kind="user", id=viewer.id), ROLE_READER) for viewer in viewers
    ]
    granted += [(Principal(kind="user", id=writer.id), ROLE_WRITER) for writer in writers]
    for principal, role in granted:
        await share_chat_with(db, chat=chat, owner=publisher, principal=principal, role=role)
    return f"doc:chat:{chat.id}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_an_event_committed_through_a_reaches_a_stream_and_a_subscriber_on_b(
    uvicorn_server_pair: tuple[str, str], real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """The publisher drives instance A over its socket; a viewer's stream and
    a viewer's socket are both on instance B. A's commit is the only thing
    that crosses: B's listener hears the outbox and fans out.

    The viewer holds "Can view" on the publisher's chat — a share written once,
    which instance B resolves from the same rows A does."""
    a, b = uvicorn_server_pair
    viewer, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    channel = await shared_chat_channel(real_session, org_admin, viewers=(viewer,))
    pub_http = await http_login(a, org_admin.admin_email, org_admin.admin_password)
    viewer_http = await http_login(b, viewer.email, password)
    async with aclosing(pub_http), aclosing(viewer_http):
        pub_ticket = await mint_ticket(pub_http)
        viewer_ticket = await mint_ticket(viewer_http)
        async with (
            connect(a, pub_ticket) as publisher,
            connect(b, viewer_ticket) as viewer_socket,
            open_stream(b, viewer_http.cookies) as stream,
        ):
            await stream.expect_connected()
            assert await publisher.subscribe(channel) is True
            await publisher.hello(channel)
            assert await viewer_socket.subscribe(channel) is False
            await viewer_socket.hello(channel)

            ack = await publisher.op(
                channel,
                epoch=1,
                intent="append",
                events=[{"event_id": "e1", "event_type": "message.created"}],
            )
            assert ack["kind"] == "ack" and ack["payload"]["seq"] == 1

            # The socket subscriber on B gets the durable op with A's sequence.
            op = await viewer_socket.next_doc("op")
            assert op["seq"] == 1 and op["peer_id"] == publisher.peer_id
            assert op["payload"]["events"][0]["event_id"] == "e1"

            # The stream on B gets the domain event A committed.
            frame = await stream.next_event_named("chat.updated")
            data = json.loads(frame["data"])
            assert data["entity_id"] == channel.split(":", 2)[2]
            assert data["org_id"] == str(org_admin.org_id) and data["version"] == 1

            # And an ephemeral chunk from A crosses to B without touching the table.
            await publisher.send(
                publisher.envelope(
                    channel,
                    "op",
                    epoch=1,
                    payload={
                        "op_id": "chunk-1",
                        "intent": "chunk",
                        "events": [{"event_id": "c1", "event_type": "agent.message_chunk"}],
                    },
                )
            )
            chunk = await viewer_socket.next_doc("op")
            assert chunk["seq"] == 0 and chunk["payload"]["intent"] == "chunk"


@pytest.mark.usefixtures("files_on")
async def test_presence_joined_on_a_is_visible_on_b_and_its_leave_propagates(
    uvicorn_server_pair: tuple[str, str], real_session: Any, org_admin: OrgWithAdmin
) -> None:
    a, b = uvicorn_server_pair
    viewer, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    channel = await shared_chat_channel(real_session, org_admin, viewers=(viewer,))
    a_http = await http_login(a, org_admin.admin_email, org_admin.admin_password)
    b_http = await http_login(b, viewer.email, password)
    async with aclosing(a_http), aclosing(b_http):
        async with (
            connect(a, await mint_ticket(a_http)) as on_a,
            connect(b, await mint_ticket(b_http)) as on_b,
        ):
            await on_a.subscribe(channel)
            await on_b.subscribe(channel)
            await on_a.send({"t": "presence.join", "channel": channel})
            joined = await on_b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
            assert joined["peers"][0]["peer_id"] == on_a.peer_id
            # A late subscriber on B reads the roster A wrote.
            async with connect(b, await mint_ticket(b_http)) as late:
                await late.send({"t": "subscribe", "channel": channel})
                assert (await late.recv())["t"] == "subscribed"
                roster = await late.recv()
                assert [p["peer_id"] for p in roster["peers"]] == [on_a.peer_id]
            await on_a.send({"t": "presence.leave", "channel": channel})
            left = await on_b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "leave")
            assert left["peers"][0]["peer_id"] == on_a.peer_id


@pytest.mark.usefixtures("files_on")
async def test_a_chat_is_refused_on_both_instances_until_the_share_is_written(
    uvicorn_server_pair: tuple[str, str], real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """Who may read a chat is the database's answer, not a replica's. An org
    member the owner shared nothing with is refused ``not_found`` on the
    instance that created the document and on the other one alike; the grant,
    written once through neither socket, admits them on both — at the rung it
    named, which is a read and not a write."""
    a, b = uvicorn_server_pair
    outsider, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    channel = await shared_chat_channel(real_session, org_admin)
    owner_http = await http_login(a, org_admin.admin_email, org_admin.admin_password)
    on_a = await http_login(a, outsider.email, password)
    on_b = await http_login(b, outsider.email, password)
    async with aclosing(owner_http), aclosing(on_a), aclosing(on_b):
        # The owner opens the document on A, so the row exists for both replicas.
        async with connect(a, await mint_ticket(owner_http)) as owner_socket:
            assert await owner_socket.subscribe(channel) is True
            await owner_socket.hello(channel)

        for addr, http in ((a, on_a), (b, on_b)):
            async with connect(addr, await mint_ticket(http)) as refused:
                error = await refused.subscribe_error(channel)
                assert error["code"] == "not_found" and error["channel"] == channel

        chat = await real_session.get(WorkspaceObject, UUID(channel.split(":", 2)[2]))
        assert chat is not None
        owner = await real_session.get(User, org_admin.admin_id)
        assert owner is not None
        await share_chat_with(
            real_session,
            chat=chat,
            owner=owner,
            principal=Principal(kind="user", id=outsider.id),
            role=ROLE_READER,
        )

        for addr, http in ((a, on_a), (b, on_b)):
            async with connect(addr, await mint_ticket(http)) as admitted:
                assert await admitted.subscribe(channel) is False, (
                    "the rung the share named reads; it does not write"
                )
                assert (await admitted.hello(channel))["payload"]["state"]["events"] == []


async def test_a_ticket_minted_on_a_opens_a_socket_on_b_once_and_is_refused_on_replay(
    uvicorn_server_pair: tuple[str, str], org_admin: OrgWithAdmin
) -> None:
    a, b = uvicorn_server_pair
    a_http = await http_login(a, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(a_http):
        ticket = await mint_ticket(a_http)
        async with connect(b, ticket) as on_b:
            await on_b.send({"t": "ping"})
            assert (await on_b.recv())["t"] == "pong"
            # Replayed on A (where it was minted) while the B socket is open: refused.
            async with connect(a, ticket, expect_welcome=False) as replay_on_a:
                assert await replay_on_a.close_code() == close_codes.UNAUTHORIZED
            # And on B again, after the first use: still refused.
            async with connect(b, ticket, expect_welcome=False) as replay_on_b:
                assert await replay_on_b.close_code() == close_codes.UNAUTHORIZED
            await on_b.send({"t": "ping"})
            assert (await on_b.recv())["t"] == "pong"


async def test_a_stream_cut_when_a_goes_away_resumes_on_b_with_no_gap_and_no_duplicate(
    org_admin: OrgWithAdmin,
) -> None:
    """Instance A serves a stream and is stopped while it is open (a deploy,
    a crash); rows land while no one listens; the client resumes on B with
    ``?after=`` — the cursor a browser-owned reconnect can carry — receiving
    exactly the rows it missed and then live delivery, nothing twice."""
    async with serve_app(create_app()) as b:
        seen_ids: list[str] = []
        async with AsyncExitStack() as clients:
            servers = AsyncExitStack()
            a = await servers.enter_async_context(serve_app())
            h = await http_login(a, org_admin.admin_email, org_admin.admin_password)
            await clients.enter_async_context(aclosing(h))
            cookies = h.cookies
            on_a = await clients.enter_async_context(open_stream(a, cookies))
            await on_a.expect_connected()
            first = await emit_row(org_admin.org_id, "before-cut")
            frame = await on_a.next_event()
            assert frame["id"] == str(first)
            seen_ids.append(frame["id"])
            # A goes away while the stream is open.
            await servers.aclose()
            assert await on_a.ended(), "the stream must end when its server stops"
        # No instance A. Rows keep landing.
        missed = [
            await emit_row(org_admin.org_id, "missed-1"),
            await emit_row(org_admin.org_id, "missed-2"),
        ]
        await emit_row(uuid4(), "another-orgs-row")
        # Resume on B from the last id the client saw.
        async with open_stream(b, cookies, params={"after": seen_ids[-1]}) as on_b:
            await on_b.expect_connected()
            replayed = [await on_b.next_event(), await on_b.next_event()]
            assert [f["id"] for f in replayed] == [str(i) for i in missed]
            assert [json.loads(f["data"])["entity_id"] for f in replayed] == [
                "missed-1",
                "missed-2",
            ]
            live = await emit_row(org_admin.org_id, "live-on-b")
            assert (await on_b.next_event())["id"] == str(live)
            ids = [f["id"] for f in on_b.frames if "id" in f]
            assert len(ids) == len(set(ids))
            assert seen_ids[-1] not in ids, "the row seen on A is not replayed on B"


async def test_a_socket_cut_when_a_goes_away_re_mints_and_re_hellos_on_b(
    org_admin: OrgWithAdmin,
) -> None:
    """A socket on A holds a chat document at epoch 1 with one durable op; A
    stops under it; the client mints a fresh ticket on B, reconnects,
    re-hellos and receives the full state — every replica reads the same
    table."""
    channel = chat_channel(org_admin)
    async with serve_app(create_app()) as b:
        servers = AsyncExitStack()
        a = await servers.enter_async_context(serve_app())
        h = await http_login(a, org_admin.admin_email, org_admin.admin_password)
        async with aclosing(h), connect(a, await mint_ticket(h)) as on_a:
            await on_a.subscribe(channel)
            await on_a.hello(channel)
            ack = await on_a.op(
                channel,
                epoch=1,
                intent="append",
                events=[{"event_id": "e1", "event_type": "message.created"}],
            )
            assert ack["payload"]["seq"] == 1
            await servers.aclose()
            code = await on_a.close_code()
            assert code != 1000, "the server going away is never a normal close"
        # The ticket that opened the A socket is spent; a fresh one opens B.
        h2 = await http_login(b, org_admin.admin_email, org_admin.admin_password)
        async with aclosing(h2), connect(b, await mint_ticket(h2)) as on_b:
            assert await on_b.subscribe(channel) is True
            snapshot = await on_b.hello(channel)
            assert snapshot["epoch"] == 1 and snapshot["seq"] == 1
            assert [e["event_id"] for e in snapshot["payload"]["state"]["events"]] == ["e1"]


async def test_a_promote_published_on_a_reaches_the_machine_on_b_and_its_ack_reaches_a(
    uvicorn_server_pair: tuple[str, str], real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """The reader's request runs on A; the box's socket is held by B. A's
    promoter publishes the request, B's listener hears it and forwards it to
    the socket, the box answers on B, and A's hub hears the answer and ends the
    reader's wait with it — nothing between them but Postgres."""
    from datetime import UTC, datetime, timedelta

    from alkera_core.authz import agent_headers
    from alkera_core.files.ids import NodeId
    from alkera_core.files.lease_snapshots import LeaseFacet
    from alkera_core.files.promotion import PromoteOutcome
    from alkera_core.models.files.tree import FileNode
    from backend.services.realtime.runtime import runtime_of
    from tests._suite_app import app as shared_app
    from tests.files._boxes import registered_box

    _a, b = uvicorn_server_pair
    runtime_a = runtime_of(shared_app)
    assert runtime_a is not None and runtime_a.promoter is not None, "A is the shared app"
    token, machine_id = await registered_box(
        real_session,
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_id=org_admin.org_id,
    )
    box_http = served_client(
        b, headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
    )
    now = datetime.now(UTC)
    lease = LeaseFacet(
        node_id=NodeId(uuid4()),
        holder_principal_id=uuid4(),
        machine=machine_id,
        purpose="chat",
        since=now,
        expires_at=now + timedelta(minutes=10),
        last_sync_at=now,
        mine=False,
        stale=False,
        live=True,
        served="live",
        epoch=5,
    )
    node = FileNode(
        id=uuid4(),
        org_team_id=org_admin.org_id,
        name=b"data.csv",
        holder_size=321,
        holder_mtime_ns=17,
    )
    async with aclosing(box_http):
        async with connect(b, await mint_ticket(box_http)) as the_box:
            await the_box.send({"t": "subscribe", "channel": f"machine:{machine_id}"})
            assert (await the_box.recv_until(lambda f: f["t"] in ("subscribed", "error")))[
                "t"
            ] == "subscribed"

            waiting = asyncio.create_task(
                runtime_a.promoter.promote(node, lease, deadline=RECV_TIMEOUT, path="in/data.csv")
            )
            request = await the_box.recv_until(lambda f: f["t"] == "machine.request")
            assert (
                request["node_id"],
                request["path"],
                request["epoch"],
                request["expected"],
            ) == (str(node.id), "in/data.csv", 5, {"size": 321, "mtime_ns": 17})
            await the_box.send(
                {"t": "machine.ack", "request_id": request["request_id"], "outcome": "missing"}
            )
            assert await asyncio.wait_for(waiting, RECV_TIMEOUT) is PromoteOutcome.MISSING


async def test_two_readers_of_one_file_on_two_replicas_are_both_served_by_one_landing(
    real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """Two readers open the same file at once, one on each replica; the box's
    socket is on B. Coalescing is per process, so each replica asks at most
    once — never once per reader — and one landing of the holder's bytes, a
    durable event every replica's listener hears, ends both waits."""
    from datetime import UTC, datetime, timedelta

    from alkera_core.authz import agent_headers
    from alkera_core.files.ids import NodeId
    from alkera_core.files.lease_snapshots import LeaseFacet
    from alkera_core.files.promotion import PromoteOutcome
    from alkera_core.models.files.tree import FileNode
    from backend.services.realtime.runtime import runtime_of
    from tests._suite_app import app as shared_app
    from tests.files._boxes import registered_box

    app_b = create_app()
    async with serve_app() as _a, serve_app(app_b) as b:
        runtime_a, runtime_b = runtime_of(shared_app), runtime_of(app_b)
        assert runtime_a is not None and runtime_a.promoter is not None
        assert runtime_b is not None and runtime_b.promoter is not None
        token, machine_id = await registered_box(
            real_session,
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_id=org_admin.org_id,
        )
        box_http = served_client(
            b, headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
        )
        now = datetime.now(UTC)
        lease = LeaseFacet(
            node_id=NodeId(uuid4()),
            holder_principal_id=uuid4(),
            machine=machine_id,
            purpose="chat",
            since=now,
            expires_at=now + timedelta(minutes=10),
            last_sync_at=now,
            mine=False,
            stale=False,
            live=True,
            served="live",
            epoch=3,
        )
        node = FileNode(
            id=uuid4(),
            org_team_id=org_admin.org_id,
            name=b"data.csv",
            holder_size=321,
            holder_mtime_ns=17,
        )
        async with aclosing(box_http):
            async with connect(b, await mint_ticket(box_http)) as the_box:
                await the_box.send({"t": "subscribe", "channel": f"machine:{machine_id}"})
                assert (await the_box.recv_until(lambda f: f["t"] in ("subscribed", "error")))[
                    "t"
                ] == "subscribed"

                readers = [
                    asyncio.create_task(
                        promoter.promote(node, lease, deadline=RECV_TIMEOUT, path="data.csv")
                    )
                    for promoter in (
                        runtime_a.promoter,
                        runtime_a.promoter,
                        runtime_b.promoter,
                        runtime_b.promoter,
                    )
                ]
                asked = [await the_box.recv_until(lambda f: f["t"] == "machine.request")]
                # Every request the replicas will send is out within a beat of
                # the first: read all of them, however many there are.
                while True:
                    try:
                        asked.append(
                            await the_box.recv_until(
                                lambda f: f["t"] == "machine.request", seconds=1.0
                            )
                        )
                    except TimeoutError:
                        break
                for request in asked:
                    await the_box.send(
                        {
                            "t": "machine.ack",
                            "request_id": request["request_id"],
                            "outcome": "accepted",
                        }
                    )
                assert not any(reader.done() for reader in readers), "every reader waits"

                async with AsyncSessionLocal() as db:
                    await emit(
                        db,
                        org_id=org_admin.org_id,
                        type=EventType.FILE_NODE_CHANGED,
                        entity="file_node",
                        entity_id=str(node.id),
                        version=2,
                        payload={"reason": "live_saved"},
                    )
                    await db.commit()
                outcomes = await asyncio.wait_for(asyncio.gather(*readers), RECV_TIMEOUT)
        assert outcomes == [PromoteOutcome.LANDED] * 4
        assert 1 <= len(asked) <= 2, "a replica asked more than once for one file"
        assert len({request["request_id"] for request in asked}) == len(asked)
