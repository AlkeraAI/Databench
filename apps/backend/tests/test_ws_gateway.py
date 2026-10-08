"""The socket gateway end to end: a real server on the test loop, real
``websockets`` clients, real Postgres. The handshake's origin check and caps,
the frame limits, the keepalive re-auth, channel authorization, presence
fan-out, and the doc-sync protocol — including the multi-user chat document:
a synthetic publisher socket and two viewer sockets from two org users see the
publisher's durable and ephemeral operations, a viewer's message is relayed to
the publisher, and a user from another org is refused opaquely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import websockets
from alkera_core.auth import revoke_all_for_user
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import MAX_PAYLOAD_BYTES, EventType, HubEvent, read_after
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import User
from alkera_core.schemas.realtime import (
    REALTIME_CLIENT_GENERATION,
    SERVER_PEER_ID,
    WS_PATH,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
    SocketLimits,
)
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from backend.services.realtime import close_codes, docsync
from backend.services.realtime.runtime import runtime_of
from backend.services.realtime.session import (
    FRAME_WINDOW_SECONDS,
    MAX_CHANNELS,
    MAX_FRAME_BYTES,
    MAX_FRAMES_PER_WINDOW,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.chat_declarations import declare_chat, redeclare_chat
from tests.chat_shares import files_on, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member, serve_controlled
from websockets.exceptions import ConnectionClosed

#: The one bound every frame wait here runs under, and it is not an assertion.
#: Each of those waits is for a signal the gateway itself sends — the welcome,
#: a close code, an in-band ``error``, a ``subscribed`` ack — and what the case
#: claims is WHICH signal came, never how many seconds it took. A short bound
#: turns the claim into one about the host instead: at eight seconds, six cases
#: here — an origin refusal, a foreign org's opaque refusal, two bad-channel
#: refusals, a team-shared chat and a forged machine ticket — failed with
#: ``TimeoutError`` inside their handshake and refusal waits on a full-suite run
#: whose load average was twenty-seven, while the file alone passed every case.
#: Every one of those refusals costs authorization reads on the one Postgres the
#: run shares, so the wait was measuring the queue in front of them.
#:
#: So the bound sits far above anything a loaded runner adds to a frame, and
#: under the suite's ninety-second per-test limit rather than at it: a socket
#: that stops answering is still a failure here — naming the frame that never
#: came, which is the whole diagnosis — while the per-test limit, which can
#: name only the test, stays the backstop of last resort behind it.
HANG_BACKSTOP = 60.0


#: Every case boots its own server on an ephemeral port, builds its own org and
#: reads the outbox from a baseline it took itself, and the process-wide caches
#: it leans on are reset on both sides of each case — so nothing here is shared
#: STATE. What is shared is the box: fifty cases, each running a real uvicorn
#: and waiting on real frames over a real socket, and the thing they assert is
#: how long a frame took. Spread across workers they ran concurrently with each
#: other and the wait stopped measuring the socket and started measuring the
#: queue in front of it — a caret case failed once that way at load 41. So they
#: name a group of their own: one worker, in series, the timings theirs.
pytestmark = [pytest.mark.compute_rows, pytest.mark.xdist_group("ws_gateway")]


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket's tick reads the shared settings object when it starts."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)


@pytest.fixture
def one_announcement_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the domain-announcement window open for the whole test, so a test
    that counts announcements over a burst of operations counts the same
    number on a loaded host as on an idle one."""
    monkeypatch.setattr(docsync, "DOMAIN_ANNOUNCE_INTERVAL_SECONDS", 300.0)


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Socket:
    """One connected peer: the raw ``websockets`` connection, its server-minted
    peer id, and frame helpers that never swallow an unexpected frame."""

    def __init__(self, ws: Any, peer_id: str) -> None:
        self.ws = ws
        self.peer_id = peer_id
        self.skipped: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(frame))

    async def recv(self, seconds: float = HANG_BACKSTOP) -> dict[str, Any]:
        return dict(json.loads(await asyncio.wait_for(self.ws.recv(), timeout=seconds)))

    async def recv_until(
        self, matches: Callable[[dict[str, Any]], bool], seconds: float = HANG_BACKSTOP
    ) -> dict[str, Any]:
        """The next frame that satisfies ``matches``; the ones before it are
        kept in ``skipped`` for the test to inspect."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.recv(max(0.05, deadline - loop.time()))
            if matches(frame):
                return frame
            self.skipped.append(frame)

    async def expect_nothing(self, seconds: float = 1.0) -> None:
        """No frame a test could assert on arrives within ``seconds``. A
        presence heartbeat or a pong is the socket keeping itself alive on the
        server's clock, not a delivery — one landing in the window is skipped,
        so the outcome never depends on where the keepalive tick fell."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            try:
                frame = await self.recv(max(0.05, deadline - loop.time()))
            except TimeoutError:
                return
            if frame.get("t") in ("presence", "pong"):
                self.skipped.append(frame)
                continue
            raise AssertionError(f"unexpected frame {frame}")

    async def close_code(self) -> int:
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout=HANG_BACKSTOP)
        except ConnectionClosed as exc:
            assert exc.rcvd is not None
            return int(exc.rcvd.code)

    async def subscribe(self, channel: str) -> bool:
        await self.send({"t": "subscribe", "channel": channel})
        subscribed = await self.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert subscribed["t"] == "subscribed", subscribed
        assert subscribed["channel"] == channel
        roster = await self.recv_until(lambda f: f["t"] == "presence" and f["event"] == "roster")
        assert roster["channel"] == channel
        return bool(subscribed["can_write"])

    async def subscribe_error(self, channel: str) -> dict[str, Any]:
        await self.send({"t": "subscribe", "channel": channel})
        frame = await self.recv_until(lambda f: f["t"] in ("subscribed", "error"))
        assert frame["t"] == "error", frame
        return frame

    def envelope(
        self, channel: str, kind: str, *, epoch: int, payload: dict[str, Any], seq: int = 0
    ) -> dict[str, Any]:
        _, doc_type, doc_id = channel.split(":", 2)
        return {
            "t": "doc",
            "envelope": {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "epoch": epoch,
                "peer_id": self.peer_id,
                "seq": seq,
                "kind": kind,
                "payload": payload,
            },
        }

    async def hello(self, channel: str) -> dict[str, Any]:
        """Send a hello; return the snapshot envelope."""
        await self.send(self.envelope(channel, "hello", epoch=0, payload={}))
        frame = await self.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("snapshot", "error")
        )
        assert frame["envelope"]["kind"] == "snapshot", frame
        assert frame["envelope"]["peer_id"] == SERVER_PEER_ID
        return dict(frame["envelope"])

    async def op(
        self, channel: str, *, epoch: int, intent: str, op_id: str | None = None, **body: Any
    ) -> dict[str, Any]:
        """Send an op; return the ack (or error / reload) envelope."""
        payload = {"op_id": op_id or f"op-{uuid4().hex[:8]}", "intent": intent, **body}
        await self.send(self.envelope(channel, "op", epoch=epoch, payload=payload))
        frame = await self.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error")
        )
        return dict(frame["envelope"])

    async def next_doc(self, kind: str, seconds: float = HANG_BACKSTOP) -> dict[str, Any]:
        frame = await self.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] == kind, seconds
        )
        return dict(frame["envelope"])


@asynccontextmanager
async def connect(
    addr: str,
    client: AsyncClient,
    *,
    origin: str | None = None,
    ticket: str | None = None,
    subprotocols: list[str] | None = None,
    expect_welcome: bool = True,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[Socket]:
    if ticket is None:
        resp = await client.post("/api/v1/ws/tickets")
        assert resp.status_code == 200, resp.text
        ticket = resp.json()["ticket"]
    offered = (
        subprotocols
        if subprotocols is not None
        else [WS_SUBPROTOCOL, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"]
    )
    async with websockets.connect(
        f"ws://{addr}{WS_PATH}",
        subprotocols=offered,
        origin=origin,
        max_size=4 * 1024 * 1024,
        additional_headers=headers,
    ) as ws:
        peer_id = ""
        if expect_welcome:
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=HANG_BACKSTOP))
            assert welcome["t"] == "welcome", welcome
            peer_id = welcome["peer_id"]
        yield Socket(ws, peer_id)


async def logged_in(email: str, password: str) -> AsyncClient:

    client = app_client()
    await login(client, email, password)
    return client


async def member_client(
    real_session: AsyncSession, org_id: UUID, *, team_id: UUID | None = None
) -> tuple[User, AsyncClient]:
    from alkera_core.models import TeamRole
    from backend.services.org import memberships as membership_service

    user, password = await make_member(real_session, org_id=org_id, verified=True)
    if team_id is not None:
        await membership_service.add_member(
            real_session, team_id=team_id, user_id=user.id, role=TeamRole.MEMBER
        )
        await real_session.commit()
    assert password is not None
    return user, await logged_in(user.email, password)


async def outbox_types_after(org_id: UUID, after: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        return [r.type for r in await read_after(db, after_id=after, org_id=org_id, limit=200)]


async def outbox_head(org_id: UUID) -> int:
    async with AsyncSessionLocal() as db:
        rows = await read_after(db, after_id=0, org_id=org_id, limit=1000)
    return max((r.id for r in rows), default=0)


def chat_channel(
    org: OrgWithAdmin, *, owner_user_id: UUID | None = None, team_id: UUID | None = None
) -> str:
    """A chat declared for ``org``, owned by its admin unless told otherwise.

    A socket cannot bring a chat into existence by asking about it, so every
    test that opens one declares it first (``declare_chat``); its document row
    is still created by the first ``hello``."""
    return declare_chat(
        org.org_id,
        owner_user_id=org.admin_id if owner_user_id is None else owner_user_id,
        team_id=team_id,
    )


async def shared_chat_channel(
    db: AsyncSession,
    org: OrgWithAdmin,
    *,
    owner: User | None = None,
    viewers: tuple[User, ...] = (),
    writers: tuple[User, ...] = (),
    teams: tuple[UUID, ...] = (),
) -> str:
    """A chat created the way ``POST /api/v1/chats`` creates it — so it has the
    Files node its ACL hangs from — shared with ``viewers`` and ``teams`` at
    "Can view" and with ``writers`` at "Can edit", returned as a channel name.

    A chat is private to whoever made it: a second member subscribing to one
    nobody shared with them is refused ``not_found``. So every test here about
    what two peers see on ONE chat has to make the share the real way — the
    grant ``files.acl.grant`` interns, not a declaration that skips the node —
    or it proves only that the door is shut.

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
    granted += [(Principal(kind="team", id=team_id), ROLE_READER) for team_id in teams]
    for principal, role in granted:
        await share_chat_with(db, chat=chat, owner=publisher, principal=principal, role=role)
    return f"doc:chat:{chat.id}"


# ---------------------------------------------------------------------------
# Handshake: origin, caps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://evil.example", id="foreign-origin"),
        pytest.param("null", id="null-origin"),
        pytest.param("HTTP://EVIL.EXAMPLE", id="foreign-origin-uppercase"),
    ],
)
async def test_a_browser_origin_outside_the_allowed_set_is_refused(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin, origin: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client, origin=origin, expect_welcome=False) as sock:
        assert await sock.close_code() == close_codes.ORIGIN_FORBIDDEN


@pytest.mark.parametrize("which", ["cors", "frontend", "absent", "cors-uppercase-host"])
async def test_an_allowed_or_absent_origin_is_admitted(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin, which: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    origin: str | None
    if which == "cors":
        origin = settings.cors_origins_list[0]
    elif which == "cors-uppercase-host":
        scheme, _, host = settings.cors_origins_list[0].partition("://")
        origin = f"{scheme}://{host.upper()}"
    elif which == "frontend":
        origin = settings.frontend_base_url.rstrip("/")
    else:
        origin = None
    async with connect(uvicorn_server, client, origin=origin) as sock:
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"


async def test_the_per_user_connection_cap_closes_4429_and_frees_on_close(
    uvicorn_server: str,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_ws_max_connections_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as first:
        async with connect(uvicorn_server, client, expect_welcome=False) as second:
            assert await second.close_code() == close_codes.TOO_MANY
        await first.send({"t": "ping"})
        assert (await first.recv())["t"] == "pong"
    # The slot came back with the close. Releasing it is the route's own work
    # after the socket ends, so the retry is for a slot that is still on its way
    # back — and a socket the cap still refuses is ACCEPTED and then closed,
    # which reaches the client as a closed connection rather than a failed
    # assertion. Both are the not-yet answer; anything else fails as itself.
    deadline = asyncio.get_running_loop().time() + HANG_BACKSTOP
    while True:
        try:
            async with connect(uvicorn_server, client) as third:
                await third.send({"t": "ping"})
                assert (await third.recv())["t"] == "pong"
            break
        except (AssertionError, ConnectionClosed):
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.05)


async def test_a_new_made_up_machine_id_per_socket_buys_no_slot(
    uvicorn_server: str,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handshake's agent headers are whatever the caller wrote. A fresh id
    per socket that verifies as no machine of the caller's is no machine: the
    socket is the person's, and the person's cap refuses the second one."""
    from alkera_core.authz import agent_headers

    monkeypatch.setattr(settings, "realtime_ws_max_connections_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client, headers=agent_headers(str(uuid4()))) as first:
        for made_up in (str(uuid4()), "sess-box-b"):
            async with connect(
                uvicorn_server, client, expect_welcome=False, headers=agent_headers(made_up)
            ) as refused:
                assert await refused.close_code() == close_codes.TOO_MANY, made_up
        await first.send({"t": "ping"})
        assert (await first.recv())["t"] == "pong"


async def test_a_refused_socket_holds_no_slot(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from backend.services.realtime.limits import ConnectionGate

    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(
        uvicorn_server, client, origin="http://evil.example", expect_welcome=False
    ) as sock:
        await sock.close_code()
    assert ConnectionGate.ws().active == 0


# ---------------------------------------------------------------------------
# Handshake: this replica must be able to deliver
# ---------------------------------------------------------------------------


async def test_a_replica_without_a_listener_refuses_the_socket_without_burning_the_ticket(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Everything a socket sees from another replica — every ``doc.op``,
    presence frame and announcement — arrives through the outbox listener, so a
    process running without one serves a socket that only ever echoes its own
    writes. That is refused at the handshake, before the ticket is read: the
    client keeps its single-use credential and spends it on a replica that can
    deliver."""
    from backend.services.realtime.runtime import runtime_of
    from tests._suite_app import app as fastapi_app

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/ws/tickets")
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["ticket"]
    runtime = runtime_of(fastapi_app)
    assert runtime is not None and runtime.listener is not None
    listener, runtime.listener = runtime.listener, None
    try:
        async with connect(uvicorn_server, client, ticket=ticket, expect_welcome=False) as refused:
            assert await refused.close_code() == close_codes.UNAVAILABLE
    finally:
        runtime.listener = listener
    async with connect(uvicorn_server, client, ticket=ticket) as sock:
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"


async def test_a_listener_that_never_connects_refuses_the_socket_and_the_stream_alike(
    uvicorn_server: str,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener that exists but holds no connection delivers nothing either.
    Both surfaces wait the same bounded moment for it and then refuse — the
    stream with 503, the socket with its own code — so a client cannot be told
    the replica is healthy on one and unusable on the other."""
    from backend.services.realtime import runtime as realtime_runtime
    from tests._suite_app import app as fastapi_app

    monkeypatch.setattr(realtime_runtime, "LISTENER_READY_SECONDS", 0.05)
    runtime = realtime_runtime.runtime_of(fastapi_app)
    assert runtime is not None and runtime.listener is not None
    monkeypatch.setattr(runtime.listener, "_connected", False)
    monkeypatch.setattr(runtime.listener, "_connected_event", asyncio.Event())
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client, expect_welcome=False) as sock:
        assert await sock.close_code() == close_codes.UNAVAILABLE
    stream = await client.get("/api/v1/events")
    assert stream.status_code == 503, "the stream and the socket judge one replica alike"


# ---------------------------------------------------------------------------
# Drain: a stopping process closes its sockets and admits no new one
# ---------------------------------------------------------------------------


async def test_stopping_the_runtime_closes_every_open_socket_1012_before_the_listener_stops(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A socket left open on a process whose listener has stopped is open and
    answers nothing, and its client cannot tell that from a quiet one — which
    is how a box sat on a draining replica through a deploy and never heard of
    a chat bound to it. The stop closes every socket with ``1012`` first, and
    only then stops the listener."""
    from backend.services.realtime import runtime as realtime_runtime
    from tests._suite_app import create_app

    application = create_app()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with serve_controlled(application) as served:
        runtime = realtime_runtime.runtime_of(application)
        assert runtime is not None and runtime.listener is not None
        listener = runtime.listener
        open_when_listener_stopped: list[int] = []
        stop_listener = listener.stop

        async def recording_stop() -> None:
            open_when_listener_stopped.append(len(runtime.sockets))
            await stop_listener()

        listener.stop = recording_stop  # type: ignore[method-assign]
        async with (
            connect(served.addr, client) as first,
            connect(served.addr, client) as second,
        ):
            for sock in (first, second):
                await sock.send({"t": "ping"})
                assert (await sock.recv())["t"] == "pong"
            assert len(runtime.sockets) == 2
            await realtime_runtime.stop(application, runtime)
            assert await first.close_code() == close_codes.SERVICE_RESTART
            assert await second.close_code() == close_codes.SERVICE_RESTART
        assert open_when_listener_stopped == [0], "the listener outlived no socket"
        assert runtime.sockets == {}


async def test_a_draining_process_refuses_a_new_socket_without_spending_its_ticket(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """While the process drains, a reconnect must land on a replica that is
    staying: the handshake is refused with ``1012`` before the ticket is read,
    so the client spends it on the next replica."""
    from backend.services.realtime import runtime as realtime_runtime

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/ws/tickets")
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["ticket"]
    runtime = realtime_runtime.runtime_of(fastapi_app)
    assert runtime is not None
    assert await realtime_runtime.drain_sockets(runtime) == 0
    try:
        async with connect(uvicorn_server, client, ticket=ticket, expect_welcome=False) as refused:
            assert await refused.close_code() == close_codes.SERVICE_RESTART
    finally:
        runtime.draining = False
    async with connect(uvicorn_server, client, ticket=ticket) as sock:
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"


# ---------------------------------------------------------------------------
# Frames: limits, unknown tags, ping
# ---------------------------------------------------------------------------


async def test_the_welcome_names_the_oldest_client_generation_the_server_serves(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A tab built before a protocol change reads this number and reloads into
    the current build; a server that stopped sending it would leave such tabs
    speaking a protocol the server no longer serves."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client, expect_welcome=False) as sock:
        welcome = json.loads(await asyncio.wait_for(sock.ws.recv(), timeout=HANG_BACKSTOP))
    assert welcome["t"] == "welcome"
    assert welcome["min_client_generation"] == REALTIME_CLIENT_GENERATION >= 1
    # A person's socket is told the budget it is held to, so a client paces
    # itself under it instead of discovering it as a close.
    assert SocketLimits.model_validate(welcome["limits"]) == SocketLimits(
        frames_per_window=MAX_FRAMES_PER_WINDOW,
        bytes_per_window=settings.realtime_ws_max_bytes_per_window,
        window_seconds=FRAME_WINDOW_SECONDS,
        max_frame_bytes=MAX_FRAME_BYTES,
        ephemeral_max_bytes=settings.realtime_ephemeral_max_bytes,
        doc_max_bytes=settings.realtime_doc_max_bytes,
        presence_ttl_seconds=settings.realtime_presence_ttl_seconds,
    )


async def test_an_oversized_frame_closes_4413(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        await sock.ws.send(json.dumps({"t": "ping", "pad": "x" * (MAX_FRAME_BYTES + 100)}))
        error = await sock.recv()
        assert error == {**error, "t": "error", "code": "frame_too_large"}
        assert await sock.close_code() == close_codes.FRAME_TOO_LARGE


async def test_a_frame_carrying_a_whole_payload_is_accepted(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The frame cap is the event log's payload ceiling plus envelope headroom,
    so a row the log accepts always fits one frame. While the two were
    independent numbers, every payload in the gap between them was answered with
    ``frame_too_large`` and a closed socket, and the box reconnected forever."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        frame = json.dumps({"t": "ping", "pad": "x" * MAX_PAYLOAD_BYTES})
        assert MAX_PAYLOAD_BYTES < len(frame.encode("utf-8")) <= MAX_FRAME_BYTES
        await sock.ws.send(frame)
        assert (await sock.recv())["t"] == "pong"


async def test_a_frame_storm_closes_4429(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from backend.services.realtime.session import MAX_FRAMES_PER_WINDOW

    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        for _ in range(MAX_FRAMES_PER_WINDOW + 5):
            await sock.ws.send(json.dumps({"t": "ping"}))
        assert await sock.close_code() == close_codes.TOO_MANY


async def test_the_box_publishing_a_chat_outsends_the_reader_window_and_stays(
    uvicorn_server: str,
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
) -> None:
    """The publisher is not held to the reader's frame rate.

    A box streaming a turn coalesces a frame every 50 ms and appends a durable
    op per harness event on top, so a fast step fills the reader window on its
    own — and closing that socket costs the chunks in the reconnect gap of a
    turn that may run for hours. So the same burst that closes a person's
    socket leaves the box's open and answering.
    """
    from backend.services.realtime.session import MAX_FRAMES_PER_WINDOW

    burst = MAX_FRAMES_PER_WINDOW + 50
    assert burst < settings.realtime_ws_publisher_max_frames_per_window, (
        "the burst must be inside the publisher window"
    )

    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        for _ in range(burst):
            await the_box.send({"t": "ping"})
        # Still there, and still reading what it is sent: the answer to a frame
        # sent AFTER the burst can only come from a socket that survived it.
        await the_box.send({"t": "not-a-frame"})
        answer = await the_box.recv_until(lambda frame: frame.get("t") == "error")
        assert answer["code"] == "unknown_frame"

    # The same burst on a person's socket is still refused: the exemption is
    # the publisher's, not everybody's.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as reader:
        for _ in range(burst):
            await reader.send({"t": "ping"})
        assert await reader.close_code() == close_codes.TOO_MANY


def test_the_publisher_frame_window_holds_a_whole_busy_turn() -> None:
    """The shipped window is far above the cadence a box publishes at.

    A box coalesces a chat's stream into a frame every 50 ms and appends a
    durable op per harness event on top, so 200 frames a window is the FLOOR a
    quiet turn sends; a turn running several tools at once sends multiples of
    it. The window only has to catch a socket looping on nothing, so it sits two
    orders of magnitude above the cadence rather than beside it — closing a
    publisher costs the turn every chunk in the reconnect gap.
    """
    from backend.services.realtime.session import (
        FRAME_WINDOW_SECONDS,
        PUBLISHER_CHUNK_INTERVAL_SECONDS,
    )

    coalesced = FRAME_WINDOW_SECONDS / PUBLISHER_CHUNK_INTERVAL_SECONDS
    assert settings.realtime_ws_publisher_max_frames_per_window >= 20_000
    assert settings.realtime_ws_publisher_max_frames_per_window >= 100 * coalesced


async def test_the_publisher_frame_window_is_what_the_operator_set(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publisher's window is a setting, not a constant: an operator who
    lowers it gets a box closed on the burst that the shipped default admits."""
    monkeypatch.setattr(settings, "realtime_ws_publisher_max_frames_per_window", 5)

    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        for _ in range(20):
            await the_box.send({"t": "ping"})
        assert await the_box.close_code() == close_codes.TOO_MANY


def _fat_frame(pad: int = MAX_PAYLOAD_BYTES) -> str:
    """One frame the size gate admits: a whole payload plus its envelope, which
    is the largest thing any client may put on the wire."""
    frame = json.dumps({"t": "ping", "pad": "x" * pad})
    assert len(frame.encode("utf-8")) <= MAX_FRAME_BYTES
    return frame


async def _send_until_closed(sock: Socket, frame: str, times: int) -> None:
    """Push ``frame`` ``times`` times, tolerating the close landing mid-burst —
    the server refusing is the point, and it need not wait for the last write."""
    with contextlib.suppress(ConnectionClosed):
        for _ in range(times):
            await sock.ws.send(frame)


async def test_a_reader_pushing_megabytes_inside_the_frame_window_closes_4429(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The frame cap and the frame-count window multiply. Three frames is
    nothing to the count window and each one is inside the size cap, yet
    together they are six megabytes of JSON for this process to parse — and a
    user may hold `realtime_ws_max_connections_per_user` sockets doing it. The
    byte window is what says no."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        await _send_until_closed(sock, _fat_frame(), 3)
        assert await sock.close_code() == close_codes.TOO_MANY


async def test_the_box_streaming_a_turn_is_not_held_to_the_readers_byte_budget(
    uvicorn_server: str,
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
) -> None:
    """A verified machine publishes a whole turn through its socket — tool
    results, files, a transcript — so the volume that is abuse from a person's
    socket is a normal minute of work for the box. It gets the larger budget
    for the same reason it gets the larger frame window."""
    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        await _send_until_closed(the_box, _fat_frame(), 3)
        # Still there, and still reading: the answer to a frame sent AFTER the
        # burst can only come from a socket that survived it.
        await the_box.send({"t": "not-a-frame"})
        answer = await the_box.recv_until(lambda frame: frame.get("t") == "error")
        assert answer["code"] == "unknown_frame"


async def test_the_readers_byte_budget_is_the_setting(
    uvicorn_server: str,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is read per connection, so an operator who narrows it gets a
    narrower socket. One frame far inside the size cap and alone in the count
    window is refused here, which only the byte term can do."""
    monkeypatch.setattr(settings, "realtime_ws_max_bytes_per_window", 4096)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        await _send_until_closed(sock, _fat_frame(pad=8192), 1)
        assert await sock.close_code() == close_codes.TOO_MANY


async def test_the_publishers_byte_budget_is_its_own_setting(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publisher lane is a larger budget, not an exemption: narrowed, it
    closes the box's socket exactly as the reader's does."""
    monkeypatch.setattr(settings, "realtime_ws_publisher_max_bytes_per_window", 4096)
    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        await _send_until_closed(the_box, _fat_frame(pad=8192), 1)
        assert await the_box.close_code() == close_codes.TOO_MANY


async def test_an_unknown_frame_is_answered_in_band_and_the_socket_stays_open(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        await sock.send({"t": "typing", "channel": "x"})
        error = await sock.recv()
        assert error["t"] == "error" and error["code"] == "unknown_frame"
        await sock.ws.send("not json")
        bad = await sock.recv()
        assert bad["t"] == "error" and bad["code"] == "bad_frame"
        await sock.send({"t": "subscribe"})
        assert (await sock.recv())["code"] == "bad_frame"
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"


# ---------------------------------------------------------------------------
# The tick: revocation, deactivation, deadline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["revoke-all", "deactivate", "session-deadline"])
async def test_the_tick_closes_a_socket_whose_session_ended(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    member, client = await member_client(real_session, org_admin.org_id)
    if how == "session-deadline":
        monkeypatch.setattr(settings, "realtime_ws_max_session_seconds", 1)
    async with connect(uvicorn_server, client) as sock:
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"
        if how == "revoke-all":
            async with AsyncSessionLocal() as db:
                await revoke_all_for_user(db, member.id)
                await db.commit()
        elif how == "deactivate":
            member.is_active = False
            await real_session.commit()
        assert await sock.close_code() == close_codes.SESSION_EXPIRED
    await client.aclose()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "code"),
    [
        pytest.param("chat:sess-1", "bad_channel", id="missing-prefix"),
        pytest.param("doc:graph:g1", "bad_channel", id="unknown-type"),
        pytest.param("doc:chat:has space", "bad_channel", id="bad-character"),
        pytest.param(f"doc:artifact:{uuid4()}", "not_found", id="artifact-that-does-not-exist"),
        pytest.param("doc:artifact:not-a-uuid", "not_found", id="artifact-id-not-a-uuid"),
    ],
)
async def test_subscribe_refuses_bad_and_unknown_channels_in_band(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin, channel: str, code: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        error = await sock.subscribe_error(channel)
        assert error["code"] == code and error["channel"] == channel
        # Still open, still usable.
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"


async def test_doc_frames_need_a_subscription_first(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    async with connect(uvicorn_server, client) as sock:
        await sock.send(sock.envelope(channel, "hello", epoch=0, payload={}))
        error = await sock.next_doc("error")
        assert error["payload"]["code"] == "not_subscribed"
        await sock.send({"t": "presence.join", "channel": channel})
        frame = await sock.recv()
        assert frame["t"] == "error" and frame["code"] == "not_subscribed"


async def test_the_channel_cap_is_enforced(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        for _ in range(MAX_CHANNELS):
            assert await sock.subscribe(chat_channel(org_admin)) is True
        error = await sock.subscribe_error(chat_channel(org_admin))
        assert error["code"] == "too_many_channels"


async def test_a_machine_holds_a_channel_for_every_chat_it_serves(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The channel cap is a person's. The box publishes for every chat it
    serves and holds one channel per chat; on the demo box the thirty-third
    chat came back ``too_many_channels`` and was never published while the
    other thirty-two sat idle. A verified machine's socket is not counted."""
    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        for _ in range(MAX_CHANNELS + 3):
            assert await the_box.subscribe(chat_channel(org_admin)) is True


async def test_a_reader_is_told_at_once_when_the_box_publishing_its_chat_comes_and_goes(
    uvicorn_server: str, client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A killed box went unnoticed for most of a minute: the machine's status
    turns only when its heartbeat lapses past the ready window. The box's socket
    closing is the first sign there is, so every reader of the chats it
    published is told the moment it happens, and told again when the box is
    back on the channel. The box itself is never sent its own news."""
    machine_id, box = await _registered_machine(real_session, org_admin)
    ticket = await _ticket_asserting(box, machine_id)
    channel = chat_channel(org_admin)
    other = chat_channel(org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with (
        connect(uvicorn_server, client) as reader,
        connect(uvicorn_server, client) as elsewhere,
    ):
        assert await reader.subscribe(channel) is True
        assert await elsewhere.subscribe(other) is True
        async with connect(uvicorn_server, box, ticket=ticket) as the_box:
            await the_box.subscribe(channel)
            here = await reader.recv_until(lambda f: f["t"] == "publisher")
            assert (here["channel"], here["state"]) == (channel, "here")
            await the_box.send({"t": "ping"})
            await the_box.recv_until(lambda f: f["t"] == "pong")
            assert not [f for f in the_box.skipped if f.get("t") == "publisher"]
        gone = await reader.recv_until(lambda f: f["t"] == "publisher")
        assert (gone["channel"], gone["state"]) == (channel, "gone")
        assert datetime.fromisoformat(gone["at"]) >= datetime.fromisoformat(here["at"])
        # A reader of a chat the box never published hears nothing about it.
        await elsewhere.expect_nothing(0.5)


async def test_a_client_claiming_the_servers_peer_id_is_stamped_with_its_own(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
) -> None:
    """Writing under the server's peer id is what says "the server wrote
    this", and what that buys is the right to name a member against a decision
    a write was approved on.

    The socket stamps every frame with the id it minted for this connection,
    so a client that sends another peer's id — stale, mistaken, or chosen — is
    overwritten rather than refused, and keeps working. That is why the
    registry's own refusal of the server id is unreachable in normal traffic,
    and why it is still worth having: the guard that makes it unreachable
    lives in one caller.
    """
    admin = await real_session.get(User, org_admin.admin_id)
    assert admin is not None
    channel = chat_channel(org_admin)
    client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    watcher_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with (
        connect(uvicorn_server, client) as sock,
        connect(uvicorn_server, watcher_client) as watcher,
    ):
        assert await sock.subscribe(channel) is True
        await sock.hello(channel)
        assert await watcher.subscribe(channel) is True
        await watcher.hello(channel)
        forged = sock.envelope(
            channel,
            "op",
            epoch=1,
            payload={
                "op_id": "op-forged",
                "intent": "append",
                "events": [{"event_id": "e-forged", "event_type": "message.created"}],
            },
        )
        forged["envelope"]["peer_id"] = SERVER_PEER_ID
        await sock.send(forged)
        ack = await sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error")
        )
        assert ack["envelope"]["kind"] == "ack", ack
        echoed = await watcher.next_doc("op")
        assert echoed["payload"]["events"][0]["event_id"] == "e-forged"
        assert echoed["peer_id"] == sock.peer_id != SERVER_PEER_ID, (
            "the frame carries the id the sender's socket was given, never the server's"
        )
    await client.aclose()
    await watcher_client.aclose()


async def test_a_user_from_another_org_is_refused_opaquely(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    """``platform_support`` is a second org: its admin may not learn that the
    first org's chat exists. A chat id is the client's own to choose and the
    org is half the document's key, so a chat the first org declared is not a
    chat the second org has — and the refusal it gets is the same, word for
    word, as the one for an id nobody anywhere has used.
    That sameness is the opacity: the subscribe is not an oracle for whether
    some other tenant holds the id. Declare the same id in the second org and
    it opens ITS document, whose transcript is empty, never the first org's."""
    chat = chat_channel(org_admin)
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    other_client = await logged_in(platform_support.admin_email, platform_support.admin_password)
    async with connect(uvicorn_server, owner_client) as owner:
        assert await owner.subscribe(chat) is True
        await owner.hello(chat)  # the doc now exists, owned by the first org
        ack = await owner.op(
            chat, epoch=1, intent="append", events=[{"event_id": "secret", "event_type": "x"}]
        )
        assert ack["kind"] == "ack"
    async with connect(uvicorn_server, other_client) as other:
        taken = await other.subscribe_error(chat)
        never_used = await other.subscribe_error(chat_channel(org_admin))
        assert {k: v for k, v in taken.items() if k != "channel"} == {
            k: v for k, v in never_used.items() if k != "channel"
        }, "the answer never reveals that another org holds the id"
        assert taken["code"] == "not_found"
    # The second org declares the SAME id: now it has a document of its own.
    redeclare_chat(chat, platform_support.org_id, owner_user_id=platform_support.admin_id)
    async with connect(uvicorn_server, other_client) as other:
        assert await other.subscribe(chat) is True
        snapshot = await other.hello(chat)
        assert snapshot["payload"]["state"]["events"] == [], "its own document, not the first org's"
    await owner_client.aclose()
    await other_client.aclose()


@pytest.mark.usefixtures("files_on")
async def test_a_chat_shared_with_a_team_is_readable_by_that_team_and_nobody_else(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat reaches a group through a grant to the team, not through the
    document row's audience: the row is created PRIVATE — it narrows to no
    team at all — and it is the team rung that admits the teammate, read-only.
    An org member outside the team, and the org admin, are ``not_found``."""
    from alkera_core.models import RealtimeDoc

    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    owner, owner_cli = await member_client(real_session, org_admin.org_id, team_id=team.id)
    _mate, mate_client = await member_client(real_session, org_admin.org_id, team_id=team.id)
    _outsider, outsider_client = await member_client(real_session, org_admin.org_id)
    admin_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    chat = await shared_chat_channel(real_session, org_admin, owner=owner, teams=(team.id,))
    async with connect(uvicorn_server, owner_cli) as owner_sock:
        assert await owner_sock.subscribe(chat) is True
        await owner_sock.hello(chat)
    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", chat.split(":", 2)[2]))
        assert doc is not None and doc.team_id is None, (
            "the row is created private — the team grant is what carries the audience"
        )
    async with connect(uvicorn_server, mate_client) as mate:
        assert await mate.subscribe(chat) is False, "the team rung reads, it does not write"
        assert (await mate.hello(chat))["payload"]["state"]["events"] == []
    async with connect(uvicorn_server, outsider_client) as outsider:
        assert (await outsider.subscribe_error(chat))["code"] == "not_found"
    async with connect(uvicorn_server, admin_client) as admin_sock:
        assert (await admin_sock.subscribe_error(chat))["code"] == "not_found", (
            "an org admin holds no rung on a chat nobody shared with them"
        )
    async with connect(uvicorn_server, owner_cli) as owner_again:
        assert await owner_again.subscribe(chat) is True
    for c in (owner_cli, mate_client, outsider_client, admin_client):
        await c.aclose()


async def _registered_machine(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> tuple[str, AsyncClient]:
    """The box registers the pod it runs on with the operator's device token,
    the way the daemon does at start. Returns the machine id and a client
    holding that token — the box's, once it also asserts the id."""
    from alkera_core.authz import agent_headers
    from tests._compute_helpers import make_grant, make_machine_type
    from tests._suite_app import app as fastapi_app
    from tests.conftest import mint_cli_token

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    box = AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    registered = await box.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": f"pod-{uuid4().hex[:8]}",
            "name": "demo-box",
            "machine_type_code": machine_type.provider_type_id,
        },
        headers=agent_headers("booting"),
    )
    assert registered.status_code == 201, registered.text
    return str(registered.json()["id"]), box


async def _ticket_asserting(client: AsyncClient, agent_id: str) -> str:
    """A socket ticket minted with the agent headers naming ``agent_id`` —
    what the daemon sends, and what anyone else can send just as easily."""
    from alkera_core.authz import agent_headers

    resp = await client.post("/api/v1/ws/tickets", headers=agent_headers(agent_id))
    assert resp.status_code == 200, resp.text
    return str(resp.json()["ticket"])


async def test_a_colleague_asserting_the_bound_machines_id_on_their_ticket_is_not_the_machine(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box's id is on every chat it serves, and the ticket carries whatever
    agent id the caller put on the mint. A colleague of the chat's owner — no
    rung, the bound machine's id on their own ticket — gets a person's socket:
    the subscribe is ``not_found``, the same answer they get with no header.
    The machine assertion is admitted only when the socket proves it is that
    machine: the operator's token naming the live machine they registered
    subscribes as the publisher; the same token naming any other id, and the
    same token naming the machine once it has been released, do not."""
    from alkera_core.models.compute import ComputeAllocation

    machine_id, box = await _registered_machine(real_session, org_admin)
    owner, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    _colleague, colleague_client = await member_client(real_session, org_admin.org_id)
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="",
        client_id=None,
        machine_id=machine_id,
        machine_status="ready",
    )
    await real_session.commit()
    channel = f"doc:chat:{chat.id}"

    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        assert await the_box.subscribe(channel) is True, "the box publishes the chat it serves"
        await the_box.hello(channel)

    ticket = await _ticket_asserting(colleague_client, machine_id)
    async with connect(uvicorn_server, colleague_client, ticket=ticket) as forged:
        assert (await forged.subscribe_error(channel))["code"] == "not_found", (
            "a colleague naming the bound machine on their own ticket is not the machine"
        )
    async with connect(uvicorn_server, colleague_client) as plain:
        assert (await plain.subscribe_error(channel))["code"] == "not_found"

    ticket = await _ticket_asserting(box, str(uuid4()))
    async with connect(uvicorn_server, box, ticket=ticket) as other_id:
        assert (await other_id.subscribe_error(channel))["code"] == "not_found", (
            "the operator's token naming a machine nobody registered"
        )

    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = "released"
        await db.commit()
    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as released:
        assert (await released.subscribe_error(channel))["code"] == "not_found", (
            "a released machine is no machine at all"
        )
    await colleague_client.aclose()
    await box.aclose()


async def test_the_box_on_the_socket_is_the_credential_it_registered_with(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box holds ONE device token: the one it registered with, and the one it
    mints every socket ticket from. The socket admits the machine assertion
    only when the ticket's session IS that registration — the operator's
    other sessions naming the same live machine get a person's socket, and a
    person holds no rung on a member's private chat. A second device token
    the operator minted on another laptop and the operator's own browser
    login both answer ``not_found``; a ticket minted from the registering
    token after those still publishes."""
    from tests._suite_app import app as fastapi_app
    from tests.conftest import mint_cli_token

    machine_id, box = await _registered_machine(real_session, org_admin)
    owner, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="",
        client_id=None,
        machine_id=machine_id,
        machine_status="ready",
    )
    await real_session.commit()
    channel = f"doc:chat:{chat.id}"

    other_token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    other_laptop = AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    browser = await logged_in(org_admin.admin_email, org_admin.admin_password)
    try:
        ticket = await _ticket_asserting(other_laptop, machine_id)
        async with connect(uvicorn_server, other_laptop, ticket=ticket) as second_token:
            assert (await second_token.subscribe_error(channel))["code"] == "not_found", (
                "a device token the operator minted elsewhere is the operator, not the box"
            )
        ticket = await _ticket_asserting(browser, machine_id)
        async with connect(uvicorn_server, browser, ticket=ticket) as cookie:
            assert (await cookie.subscribe_error(channel))["code"] == "not_found", (
                "the operator's browser session naming the machine is not the box"
            )
        ticket = await _ticket_asserting(box, machine_id)
        async with connect(uvicorn_server, box, ticket=ticket) as the_box:
            assert await the_box.subscribe(channel) is True, (
                "the ticket minted from the registering token is the box"
            )
            await the_box.hello(channel)
    finally:
        await other_laptop.aclose()
        await browser.aclose()
        await box.aclose()


# ---------------------------------------------------------------------------
# Presence over the socket
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_presence_join_heartbeat_and_leave_reach_the_other_peer(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    member, member_client_ = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(member,))
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with (
        connect(uvicorn_server, owner_client) as a,
        connect(uvicorn_server, member_client_) as b,
    ):
        await a.subscribe(channel)
        await b.subscribe(channel)
        await a.send({"t": "presence.join", "channel": channel})
        seen_by_b = await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        assert seen_by_b["peers"][0]["peer_id"] == a.peer_id
        assert seen_by_b["peers"][0]["user_id"] == str(org_admin.admin_id)
        seen_by_a = await a.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        assert seen_by_a["peers"][0]["peer_id"] == a.peer_id
        # A late subscriber's roster lists the joined peer.
        async with connect(uvicorn_server, member_client_) as c:
            await c.send({"t": "subscribe", "channel": channel})
            assert (await c.recv())["t"] == "subscribed"
            roster = await c.recv()
            assert roster["event"] == "roster"
            assert [p["peer_id"] for p in roster["peers"]] == [a.peer_id]
        await a.send({"t": "presence.heartbeat", "channel": channel})
        beat = await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "heartbeat")
        assert beat["peers"][0]["peer_id"] == a.peer_id
        await a.send({"t": "presence.leave", "channel": channel})
        left = await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "leave")
        assert left["peers"][0]["peer_id"] == a.peer_id
    await owner_client.aclose()
    await member_client_.aclose()


async def test_a_heartbeat_held_past_the_lock_timeout_costs_a_beat_not_the_socket(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A busy database used to end the tick loop, so every open tab was closed
    1011 at once (``realtime.ws.loop_failed``) and reconnected into the same
    queue. The presence row is held by another session past ``lock_timeout``,
    which is exactly the error the tick's heartbeat met; the socket must stay
    open and keep beating once the row comes free."""
    member, member_client_ = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(member,))
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    held_for = settings.database_lock_timeout_ms / 1000 + 2.0
    async with (
        connect(uvicorn_server, owner_client) as a,
        connect(uvicorn_server, member_client_) as b,
    ):
        await a.subscribe(channel)
        await b.subscribe(channel)
        await a.send({"t": "presence.join", "channel": channel})
        await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        async with AsyncSessionLocal() as holder:
            locked = await holder.execute(
                text("SELECT 1 FROM realtime_presence WHERE peer_id = :peer FOR UPDATE"),
                {"peer": a.peer_id},
            )
            assert locked.scalar_one() == 1
            await asyncio.sleep(held_for)
            await holder.rollback()
        await a.send({"t": "ping"})
        assert (await a.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
        beat = await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "heartbeat")
        assert beat["peers"][0]["peer_id"] == a.peer_id
    await owner_client.aclose()
    await member_client_.aclose()


@pytest.mark.usefixtures("files_on")
async def test_a_caret_reaches_the_other_peer_and_touches_no_presence_row(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Where a peer's caret is rides the ephemeral lane as a ``presence``
    delta: the other peer sees it on the sender's peer, stamped with the
    sender's name so a caret flag can be drawn without a second lookup, and
    the presence table is not touched — neither the row's ``last_seen_at``
    nor its count moves, so a caret is never mistaken for a heartbeat."""
    from alkera_core.models import RealtimePresence
    from backend.services.realtime import presence
    from backend.services.realtime.channels import Channel
    from sqlalchemy import select

    member, member_client_ = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(member,))
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    chan = Channel(doc_type="chat", doc_id=channel.split(":", 2)[2])

    async def last_seen(peer_id: str) -> Any:
        real_session.expire_all()
        return (
            await real_session.execute(
                select(RealtimePresence.last_seen_at).where(
                    RealtimePresence.doc_type == chan.doc_type,
                    RealtimePresence.doc_id == chan.doc_id,
                    RealtimePresence.peer_id == peer_id,
                )
            )
        ).scalar_one()

    async with (
        connect(uvicorn_server, owner_client) as a,
        connect(uvicorn_server, member_client_) as b,
    ):
        await a.subscribe(channel)
        await b.subscribe(channel)
        # A caret before joining has no face to belong to: told, not joined.
        await a.send(
            {"t": "presence.cursor", "channel": channel, "cursor": {"offset": 3, "anchor": 3}}
        )
        refused = await a.recv_until(lambda f: f["t"] == "error")
        assert refused["code"] == "not_joined"
        assert refused["channel"] == channel
        await a.send({"t": "presence.join", "channel": channel})
        await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        await a.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        before = await last_seen(a.peer_id)

        await a.send(
            {
                "t": "presence.cursor",
                "channel": channel,
                "cursor": {"offset": 7, "anchor": 2, "before": "what ab", "after": "out"},
            }
        )
        seen = await b.recv_until(lambda f: f["t"] == "presence" and f["event"] == "cursor")
        assert seen["channel"] == channel
        (peer,) = seen["peers"]
        assert peer["peer_id"] == a.peer_id
        assert peer["user_id"] == str(org_admin.admin_id)
        assert peer["display_name"] != ""
        assert {k: peer["cursor"][k] for k in ("offset", "anchor", "before", "after")} == {
            "offset": 7,
            "anchor": 2,
            "before": "what ab",
            "after": "out",
        }
        # The sender hears its own caret too (the emitting replica receives
        # its own notification); a client draws only the carets that are not
        # its own.
        own = await a.recv_until(lambda f: f["t"] == "presence" and f["event"] == "cursor")
        assert own["peers"][0]["peer_id"] == a.peer_id

        assert await last_seen(a.peer_id) == before
        assert await presence.count_for_channel(real_session, chan, org_admin.org_id) == 1

        # A caret that is not a caret is refused in band and the socket lives.
        await a.send(
            {"t": "presence.cursor", "channel": channel, "cursor": {"offset": -1, "anchor": 0}}
        )
        bad = await a.recv_until(lambda f: f["t"] == "error")
        assert bad["code"] == "bad_frame"
        await a.send({"t": "ping"})
        assert (await a.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
    await owner_client.aclose()
    await member_client_.aclose()


@pytest.mark.usefixtures("files_on")
async def test_a_caret_on_a_channel_the_socket_never_subscribed_is_refused(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    channel = await shared_chat_channel(real_session, org_admin, viewers=())
    async with connect(uvicorn_server, owner_client) as a:
        await a.send(
            {"t": "presence.cursor", "channel": channel, "cursor": {"offset": 0, "anchor": 0}}
        )
        refused = await a.recv_until(lambda f: f["t"] == "error")
        assert refused["code"] == "not_subscribed"
        assert refused["channel"] == channel
    await owner_client.aclose()


# ---------------------------------------------------------------------------
# Doc-sync: hello / snapshot / ops / acks
# ---------------------------------------------------------------------------


async def test_hello_returns_a_full_snapshot_from_the_server_peer(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    async with connect(uvicorn_server, client) as sock:
        assert await sock.subscribe(channel) is True
        snapshot = await sock.hello(channel)
        assert snapshot["epoch"] == 1 and snapshot["seq"] == 0
        assert snapshot["payload"]["seq"] == 0
        assert snapshot["payload"]["state"] == {
            "schema_version": "1.0.0",
            "meta": {"session_id": channel.split(":", 2)[2]},
            "events": [],
            "ids": {},
        }


async def test_a_document_written_without_a_schema_version_is_brought_forward_on_hello(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A row an older writer left without a state version is stamped and
    re-epoched on the next hello; the socket sees the reload and a snapshot
    that carries the version and the transcript."""
    from alkera_core.models import RealtimeDoc

    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    doc_id = channel.split(":", 2)[2]
    async with connect(uvicorn_server, client) as sock:
        await sock.subscribe(channel)
        assert (await sock.hello(channel))["epoch"] == 1
        async with AsyncSessionLocal() as db:
            doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", doc_id))
            assert doc is not None
            doc.state = {
                "meta": {"session_id": doc_id},
                "events": [{"event_id": "e1", "event_type": "message.created"}],
                "ids": {"e1": 0},
            }
            await db.commit()
        again = await sock.hello(channel)
        assert (again["epoch"], again["seq"]) == (2, 0)
        assert again["payload"]["state"]["schema_version"] == "1.0.0"
        assert [e["event_id"] for e in again["payload"]["state"]["events"]] == ["e1"]
        reload = await sock.next_doc("reload")
        assert reload["epoch"] == 2 and reload["payload"]["reason"] == "schema_upgrade"


async def test_a_reserved_or_server_only_kind_from_a_client_is_refused(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    async with connect(uvicorn_server, client) as sock:
        await sock.subscribe(channel)
        for kind in ("crdt", "ack", "reload", "error", "presence"):
            await sock.send(sock.envelope(channel, kind, epoch=1, payload={}))
            error = await sock.next_doc("error")
            assert error["payload"]["code"] == "unsupported_kind", kind


@pytest.mark.usefixtures("files_on")
async def test_a_chat_document_is_multi_user_publisher_writes_viewers_read(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    one_announcement_window: None,
) -> None:
    """A publisher (the org admin) and three org members the chat is shared
    with at "Can view": durable appends reach both viewers with the server
    sequence, ephemeral chunks reach both without touching the outbox, a
    viewer's message is relayed to the publisher, a late viewer's snapshot
    holds the durable history and none of the chunks, and no viewer may
    append — a rung reads, only the publisher writes the transcript."""
    a, a_client = await member_client(real_session, org_admin.org_id)
    b, b_client = await member_client(real_session, org_admin.org_id)
    c, c_client = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(b, c), writers=(a,))
    doc_id = channel.split(":", 2)[2]
    publisher_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    before = await outbox_head(org_admin.org_id)
    async with (
        connect(uvicorn_server, publisher_client) as pub,
        connect(uvicorn_server, a_client) as viewer_a,
        connect(uvicorn_server, b_client) as viewer_b,
    ):
        assert await pub.subscribe(channel) is True
        snapshot = await pub.hello(channel)
        assert snapshot["epoch"] == 1
        assert await viewer_a.subscribe(channel) is True, '"Can edit" is told it may send'
        assert await viewer_b.subscribe(channel) is False
        assert (await viewer_a.hello(channel))["payload"]["state"]["events"] == []
        assert (await viewer_b.hello(channel))["payload"]["state"]["events"] == []

        # A durable append: the publisher gets the ack, both viewers the op.
        ack = await pub.op(
            channel,
            epoch=1,
            intent="append",
            op_id="op-1",
            events=[{"event_id": "e1", "event_type": "message.created"}],
        )
        assert ack["kind"] == "ack"
        assert ack["payload"] == {**ack["payload"], "op_id": "op-1", "seq": 1, "changed": True}
        for viewer in (viewer_a, viewer_b):
            op = await viewer.next_doc("op")
            assert op["seq"] == 1 and op["epoch"] == 1 and op["peer_id"] == pub.peer_id
            assert op["payload"]["intent"] == "append"
            assert op["payload"]["events"][0]["event_id"] == "e1"
            # The transcript sequence the durable write assigned, which the
            # publisher could not know and each viewer needs to be able to name
            # the row it just took — and so to let it go when its loaded window
            # is full. The document's ``seq`` above is the operation's; this is
            # the entry's place in the transcript.
            assert op["payload"]["events"][0]["seq"] == 1

        # An ephemeral chunk: no ack, both viewers see it at seq 0, no outbox row.
        await pub.send(
            pub.envelope(
                channel,
                "op",
                epoch=1,
                payload={
                    "op_id": "chunk-1",
                    "intent": "chunk",
                    "events": [
                        {"event_id": "c1", "event_type": "agent.message_chunk", "delta": "Th"}
                    ],
                },
            )
        )
        for viewer in (viewer_a, viewer_b):
            chunk = await viewer.next_doc("op")
            assert chunk["seq"] == 0 and chunk["payload"]["intent"] == "chunk"
            assert chunk["payload"]["events"][0]["delta"] == "Th"
        await pub.expect_nothing(0.5)

        # The "Can edit" rung's prompt is relayed to the publisher (and the
        # other viewer) under its own name — and moves no state.
        relay_ack = await viewer_a.op(
            channel,
            epoch=1,
            intent="user_message",
            op_id="msg-1",
            events=[{"event_id": "u1", "kind": "prompt", "text": "hello", "client_id": "m1"}],
        )
        assert relay_ack["kind"] == "ack" and relay_ack["payload"]["changed"] is False
        relayed = await pub.next_doc("op")
        assert relayed["payload"]["intent"] == "user_message"
        assert relayed["peer_id"] == viewer_a.peer_id
        assert relayed["payload"]["events"][0]["text"] == "hello"
        assert relayed["payload"]["events"][0]["user_id"] == str(a.id)
        assert (await viewer_b.next_doc("op"))["payload"]["intent"] == "user_message"

        # A "Can view" rung's prompt does not meet the send gate at all.
        unsent = await viewer_b.op(
            channel,
            epoch=1,
            intent="user_message",
            events=[{"event_id": "u2", "kind": "prompt", "text": "me too", "client_id": "m2"}],
        )
        assert unsent["kind"] == "error" and unsent["payload"]["code"] == "forbidden"

        # No rung appends to the transcript — not even "Can edit", which may
        # send: the publisher is the only peer whose events become history.
        for viewer in (viewer_a, viewer_b):
            denied = await viewer.op(
                channel,
                epoch=1,
                intent="append",
                events=[{"event_id": "e2", "event_type": "message.created"}],
            )
            assert denied["kind"] == "error" and denied["payload"]["code"] == "forbidden"
        denied_meta = await viewer_b.op(channel, epoch=1, intent="set_meta", meta={"title": "x"})
        assert denied_meta["payload"]["code"] == "forbidden"

        # The publisher's meta write reaches the viewers and emits chat.updated.
        meta_ack = await pub.op(channel, epoch=1, intent="set_meta", meta={"title": "Renamed"})
        assert meta_ack["payload"]["seq"] == 2
        for viewer in (viewer_a, viewer_b):
            assert (await viewer.next_doc("op"))["payload"]["intent"] == "set_meta"

        # A late viewer sees the durable history and none of the chunks.
        async with connect(uvicorn_server, c_client) as late:
            assert await late.subscribe(channel) is False
            state = (await late.hello(channel))["payload"]["state"]
            assert [e["event_id"] for e in state["events"]] == ["e1"]
            assert [e["seq"] for e in state["events"]] == [1], "the two lanes agree on the row"
            assert state["meta"]["title"] == "Renamed"
            assert state["meta"]["session_id"] == doc_id

    types = await outbox_types_after(org_admin.org_id, before)
    assert types.count(EventType.DOC_OP.value) == 3, "append, relay, set_meta — never the chunk"
    # Every subscriber saw all three ops above. The domain event is for the
    # rest of the org, and both durable writes fall in one announcement
    # window, so the org is asked to refetch once (see test_docsync.py).
    assert types.count(EventType.CHAT_UPDATED.value) == 1
    for c in (publisher_client, a_client, b_client, c_client):
        await c.aclose()


async def test_a_grant_from_before_the_document_existed_is_refused_once_another_org_owns_it(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    """Two orgs declare a chat under the SAME client-chosen id, and org B
    subscribes before either document exists. B never reaches A's document:
    before B has one of its own the ephemeral lane answers not_found, and B's
    hello creates B's OWN row under that id. The two documents are strangers —
    neither socket sees the other's operations and neither transcript carries
    the other's event."""
    from alkera_core.models import RealtimeDoc

    chat = chat_channel(org_admin)
    redeclare_chat(chat, platform_support.org_id, owner_user_id=platform_support.admin_id)
    doc_id = chat.split(":", 2)[2]
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    other_client = await logged_in(platform_support.admin_email, platform_support.admin_password)
    async with (
        connect(uvicorn_server, other_client) as outsider,
        connect(uvicorn_server, owner_client) as owner,
    ):
        assert await outsider.subscribe(chat) is True, "org B's own chat, not org A's"
        assert await owner.subscribe(chat) is True
        await owner.hello(chat)
        await outsider.send(
            outsider.envelope(
                chat,
                "op",
                epoch=1,
                payload={
                    "op_id": "c",
                    "intent": "chunk",
                    "events": [{"event_id": "c1", "event_type": "agent.message_chunk"}],
                },
            )
        )
        assert (await outsider.next_doc("error"))["payload"]["code"] == "not_found", (
            "A's document is not B's to stream on"
        )
        snapshot = await outsider.hello(chat)
        assert snapshot["epoch"] == 1 and snapshot["payload"]["state"]["events"] == []
        theirs = await outsider.op(
            chat, epoch=1, intent="append", events=[{"event_id": "b1", "event_type": "x"}]
        )
        assert theirs["kind"] == "ack"
        ack = await owner.op(
            chat, epoch=1, intent="append", events=[{"event_id": "e1", "event_type": "x"}]
        )
        assert ack["kind"] == "ack"
        await outsider.expect_nothing(1.0)
        await owner.expect_nothing(1.0)
    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", doc_id))
        assert doc is not None
        assert (doc.org_id, doc.owner_user_id, doc.epoch, doc.seq) == (
            org_admin.org_id,
            org_admin.admin_id,
            1,
            1,
        )
        assert [e["event_id"] for e in doc.state["events"]] == ["e1"]
        mine = await db.get(RealtimeDoc, (platform_support.org_id, "chat", doc_id))
        assert mine is not None
        assert (mine.org_id, mine.owner_user_id) == (
            platform_support.org_id,
            platform_support.admin_id,
        )
        assert [e["event_id"] for e in mine.state["events"]] == ["b1"]
    await owner_client.aclose()
    await other_client.aclose()


async def test_a_grant_from_before_the_document_existed_does_not_let_a_non_owner_write(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Same org: a member subscribes to a chat declared to them, the chat is
    handed to someone else before any document exists, and the new owner's
    hello creates it. Without a hello of its own the member's writes, rebuild
    and stream are refused from the row, and the first path that reads the row
    corrects what the member was told at subscribe time."""
    from alkera_core.models import RealtimeDoc

    viewer_user, viewer_client = await member_client(real_session, org_admin.org_id)
    chat = chat_channel(org_admin, owner_user_id=viewer_user.id)
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with (
        connect(uvicorn_server, viewer_client) as viewer,
        connect(uvicorn_server, owner_client) as owner,
    ):
        assert await viewer.subscribe(chat) is True, "declared to the member as they subscribed"
        redeclare_chat(chat, org_admin.org_id, owner_user_id=org_admin.admin_id)
        assert await owner.subscribe(chat) is True
        await owner.hello(chat)
        denied = await viewer.op(
            chat, epoch=1, intent="append", events=[{"event_id": "e9", "event_type": "x"}]
        )
        assert denied["kind"] == "error" and denied["payload"]["code"] == "forbidden"
        denied_meta = await viewer.op(chat, epoch=1, intent="set_meta", meta={"title": "mine"})
        assert denied_meta["payload"]["code"] == "forbidden"
        await viewer.send(
            viewer.envelope(chat, "snapshot", epoch=1, payload={"state": {}, "seq": 0})
        )
        assert (await viewer.next_doc("error"))["payload"]["code"] == "forbidden"
        await viewer.send(
            viewer.envelope(
                chat,
                "op",
                epoch=1,
                payload={
                    "op_id": "c",
                    "intent": "chunk",
                    "events": [{"event_id": "c1", "event_type": "agent.message_chunk"}],
                },
            )
        )
        # The stream is the first path to read the row for this socket: the
        # refusal comes with the corrected subscription.
        corrected = await viewer.recv_until(lambda f: f["t"] == "subscribed")
        assert corrected == {**corrected, "channel": chat, "can_write": False}
        assert (await viewer.next_doc("error"))["payload"]["code"] == "forbidden"
        await owner.expect_nothing(0.5)
        await viewer.send(viewer.envelope(chat, "hello", epoch=0, payload={}))
        assert (await viewer.next_doc("snapshot"))["epoch"] == 1
        assert not [f for f in viewer.skipped if f["t"] == "subscribed"], (
            "told once, not again on the hello"
        )
    async with AsyncSessionLocal() as db:
        doc = await db.get(RealtimeDoc, (org_admin.org_id, "chat", chat.split(":", 2)[2]))
        assert doc is not None and (doc.epoch, doc.seq) == (1, 0)
        assert doc.state["events"] == [] and "title" not in doc.state["meta"]
    await owner_client.aclose()
    await viewer_client.aclose()


async def test_chat_append_dedupes_by_event_id_across_ops(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    async with connect(uvicorn_server, client) as sock:
        await sock.subscribe(channel)
        await sock.hello(channel)
        first = await sock.op(
            channel, epoch=1, intent="append", events=[{"event_id": "e1", "event_type": "x"}]
        )
        assert first["payload"]["seq"] == 1 and first["payload"]["changed"] is True
        replay = await sock.op(
            channel, epoch=1, intent="append", events=[{"event_id": "e1", "event_type": "x"}]
        )
        assert replay["payload"]["seq"] == 1 and replay["payload"]["changed"] is False
        state = (await sock.hello(channel))["payload"]["state"]
        assert [e["event_id"] for e in state["events"]] == ["e1"]


async def test_a_stale_epoch_op_gets_error_then_reload_and_a_fresh_hello_recovers(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    async with connect(uvicorn_server, client) as pub, connect(uvicorn_server, client) as other:
        await pub.subscribe(channel)
        await pub.hello(channel)
        await other.subscribe(channel)
        assert (await other.hello(channel))["epoch"] == 1
        # The publisher rebuilds: every subscriber is told to reload at epoch 2.
        await pub.send(
            pub.envelope(
                channel,
                "snapshot",
                epoch=1,
                payload={"state": {"meta": {}, "events": [], "ids": {}}, "seq": 0},
            )
        )
        reload_seen = await other.next_doc("reload")
        assert (
            reload_seen["epoch"] == 2 and reload_seen["payload"]["reason"] == "publisher_snapshot"
        )
        assert (await pub.next_doc("reload"))["epoch"] == 2
        # A peer that still speaks epoch 1 is refused and told the current epoch.
        stale = await other.op(
            channel, epoch=1, intent="user_message", events=[{"event_id": "u", "event_type": "x"}]
        )
        assert stale["kind"] == "error" and stale["payload"]["code"] == "stale_epoch"
        assert stale["epoch"] == 2
        reload = await other.next_doc("reload")
        assert reload["epoch"] == 2 and reload["payload"]["reason"] == "stale_epoch"
        fresh = await other.hello(channel)
        assert fresh["epoch"] == 2 and fresh["seq"] == 0
        ok = await other.op(
            channel, epoch=2, intent="user_message", events=[{"event_id": "u", "event_type": "x"}]
        )
        assert ok["kind"] == "ack"


@pytest.mark.usefixtures("files_on")
async def test_a_publisher_snapshot_requires_write(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A "Can view" rung is admitted to the chat and still may not replace the
    document: the publisher's snapshot frame is the publisher's alone."""
    viewer, viewer_client = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(viewer,))
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, owner_client) as owner:
        await owner.subscribe(channel)
        await owner.hello(channel)
        async with connect(uvicorn_server, viewer_client) as viewer:
            await viewer.subscribe(channel)
            await viewer.send(
                viewer.envelope(channel, "snapshot", epoch=1, payload={"state": {}, "seq": 0})
            )
            assert (await viewer.next_doc("error"))["payload"]["code"] == "forbidden"
    await owner_client.aclose()
    await viewer_client.aclose()


# ---------------------------------------------------------------------------
# Chat document limits
# ---------------------------------------------------------------------------


async def test_a_hello_past_the_document_ceiling_is_refused_in_band_and_the_socket_lives(
    uvicorn_server: str,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat id is the client's own, so an org holds at most so many chat
    documents. The hello that would mint one too many is answered on the
    channel with ``quota_exceeded`` — no row written — and the socket keeps
    serving: it still pongs, and the document it already has still takes ops."""
    from alkera_core.models import RealtimeDoc

    monkeypatch.setattr(settings, "realtime_docs_max_per_org", 1)
    first, second = chat_channel(org_admin), chat_channel(org_admin)
    client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        assert await sock.subscribe(first) is True
        assert (await sock.hello(first))["epoch"] == 1
        assert await sock.subscribe(second) is True, "the refusal comes at the hello, not before"
        await sock.send(sock.envelope(second, "hello", epoch=0, payload={}))
        refusal = await sock.next_doc("error")
        assert refusal["payload"]["code"] == "quota_exceeded"
        await sock.send({"t": "ping"})
        assert (await sock.recv())["t"] == "pong"
        ack = await sock.op(
            first, epoch=1, intent="append", events=[{"event_id": "e1", "event_type": "x"}]
        )
        assert ack["kind"] == "ack"
    async with AsyncSessionLocal() as db:
        assert (
            await db.get(RealtimeDoc, (org_admin.org_id, "chat", second.split(":", 2)[2])) is None
        )
        kept = await db.get(RealtimeDoc, (org_admin.org_id, "chat", first.split(":", 2)[2]))
        assert kept is not None and kept.seq == 1
    await client.aclose()


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_an_op_carrying_a_number_json_cannot_store_is_refused_and_the_socket_lives(
    uvicorn_server: str, org_admin: OrgWithAdmin, literal: str
) -> None:
    """``NaN`` and the infinities are not JSON, but every reader in the chain
    accepts their literals, so a client can put one on the wire. Postgres
    cannot store it: unrefused, it reached the row's state and took the whole
    transaction — and the socket — down. It is a bad operation, answered in
    band, and the connection carries on serving the document."""
    channel = chat_channel(org_admin)
    client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, client) as sock:
        await sock.subscribe(channel)
        assert (await sock.hello(channel))["seq"] == 0
        frame = sock.envelope(
            channel,
            "op",
            epoch=1,
            payload={
                "op_id": "op-nonfinite",
                "intent": "append",
                "events": [{"event_id": "e1", "event_type": "message.created", "score": literal}],
            },
        )
        # ``json.dumps`` writes these literals unquoted, exactly as a browser's
        # ``JSON.stringify`` would not — but any non-browser client can.
        await sock.ws.send(json.dumps(frame).replace(f'"{literal}"', literal))
        answer = await sock.recv_until(
            lambda f: f["t"] == "doc" and f["envelope"]["kind"] in ("ack", "error")
        )
        assert answer["envelope"]["kind"] == "error"
        assert answer["envelope"]["payload"]["code"] == "bad_op"
        await sock.send({"t": "ping"})
        assert (await sock.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
        assert (await sock.hello(channel))["seq"] == 0
    await client.aclose()


# ---------------------------------------------------------------------------
# Peer identity: the envelope carries the socket's id, whatever the client wrote
# ---------------------------------------------------------------------------


def _signed_as(frame: dict[str, Any], peer_id: str) -> dict[str, Any]:
    """``frame`` with its envelope's peer id replaced — a client's forgery."""
    return {**frame, "envelope": {**frame["envelope"], "peer_id": peer_id}}


@pytest.mark.usefixtures("files_on")
async def test_a_client_asserted_peer_id_is_replaced_with_the_sockets_own(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A viewer naming another viewer neither speaks as it in the relay nor
    silences the delivery to it; a publisher naming a viewer does not keep
    its op from that viewer; a stream carries the streamer's id; and the
    event log records who really sent each op."""
    a, a_client = await member_client(real_session, org_admin.org_id)
    b, b_client = await member_client(real_session, org_admin.org_id)
    channel = await shared_chat_channel(real_session, org_admin, viewers=(b,), writers=(a,))
    publisher_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    before = await outbox_head(org_admin.org_id)
    async with (
        connect(uvicorn_server, publisher_client) as pub,
        connect(uvicorn_server, a_client) as viewer_a,
        connect(uvicorn_server, b_client) as viewer_b,
    ):
        for sock in (pub, viewer_a, viewer_b):
            await sock.subscribe(channel)
            await sock.hello(channel)
        assert len({pub.peer_id, viewer_a.peer_id, viewer_b.peer_id}) == 3

        relay = viewer_a.envelope(
            channel,
            "op",
            epoch=1,
            payload={
                "op_id": "m1",
                "intent": "user_message",
                "events": [
                    {"event_id": "u1", "kind": "prompt", "text": "hello", "client_id": "m1"}
                ],
            },
        )
        await viewer_a.send(_signed_as(relay, viewer_b.peer_id))
        assert (await viewer_a.next_doc("ack"))["payload"]["op_id"] == "m1"
        assert (await pub.next_doc("op"))["peer_id"] == viewer_a.peer_id
        seen_by_b = await viewer_b.next_doc("op")
        assert seen_by_b["peer_id"] == viewer_a.peer_id, "the forgery did not silence b"
        await viewer_a.expect_nothing(0.5)  # and the sender still gets no echo

        append = pub.envelope(
            channel,
            "op",
            epoch=1,
            payload={
                "op_id": "p1",
                "intent": "append",
                "events": [{"event_id": "e1", "event_type": "message.created"}],
            },
        )
        await pub.send(_signed_as(append, viewer_a.peer_id))
        assert (await pub.next_doc("ack"))["payload"]["seq"] == 1
        for viewer in (viewer_a, viewer_b):
            op = await viewer.next_doc("op")
            assert (op["peer_id"], op["seq"]) == (pub.peer_id, 1)

        chunk = pub.envelope(
            channel,
            "op",
            epoch=1,
            payload={
                "op_id": "c1",
                "intent": "chunk",
                "events": [{"event_id": "c1", "event_type": "agent.message_chunk", "delta": "x"}],
            },
        )
        await pub.send(_signed_as(chunk, viewer_b.peer_id))
        for viewer in (viewer_a, viewer_b):
            streamed = await viewer.next_doc("op")
            assert streamed["payload"]["intent"] == "chunk"
            assert streamed["peer_id"] == pub.peer_id
        await pub.expect_nothing(0.5)

    async with AsyncSessionLocal() as db:
        rows = await read_after(db, after_id=before, org_id=org_admin.org_id, limit=100)
    signed = [r.payload["envelope"]["peer_id"] for r in rows if r.type == EventType.DOC_OP.value]
    assert signed == [viewer_a.peer_id, pub.peer_id]
    for c in (publisher_client, a_client, b_client):
        await c.aclose()


# ---------------------------------------------------------------------------
# Overflow
# ---------------------------------------------------------------------------


def _elsewhere_op(org_id: UUID, channel: str, *, seq: int) -> HubEvent:
    """One doc operation from another peer, shaped exactly as the listener
    publishes an outbox row (:meth:`HubEvent.from_outbox`)."""
    _, _, doc_id = channel.split(":", 2)
    return HubEvent(
        lane="durable",
        org_id=org_id,
        type=EventType.DOC_OP.value,
        entity="doc",
        entity_id=channel,
        version=seq,
        visibility="org",
        payload={
            "envelope": {
                "doc_id": doc_id,
                "doc_type": "chat",
                "epoch": 1,
                "peer_id": "p:elsewhere",
                "seq": seq,
                "kind": "op",
                "payload": {"op_id": f"o{seq}", "intent": "append", "events": []},
            }
        },
        id=seq,
        channel=channel,
    )


async def test_a_subscriber_that_falls_behind_gets_a_reset_frame(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A socket that cannot keep up is told so once and keeps its place: the
    events it could not hold are discarded rather than delivered late, and the
    next one arrives normally because reading the marker is the acknowledgement.

    The burst is published straight onto the served app's hub — the injection
    the socket fixtures already sanction, on the one loop everything here runs
    on — rather than committed through the outbox. Falling behind is the
    premise of the case, not its claim, and ``publish`` never awaits, so a
    burst larger than the queue overflows it in a single loop iteration with
    the reader parked. Written as rows instead, the same premise costs
    two-hundred-odd inserts, as many notifications and a catch-up read on the
    one Postgres the run shares, and the case becomes a claim about how fast
    this host does all that: it timed out on two of four full-suite runs while
    passing alone every time.
    """
    from alkera_core.events.hub import DEFAULT_QUEUE_MAXSIZE

    await login(client, org_admin.admin_email, org_admin.admin_password)
    channel = chat_channel(org_admin)
    runtime = runtime_of(fastapi_app)
    assert runtime is not None, "the served app's lifespan binds the hub this publishes to"
    async with connect(uvicorn_server, client) as sock:
        await sock.subscribe(channel)
        await sock.hello(channel)
        sock.skipped.clear()
        for i in range(DEFAULT_QUEUE_MAXSIZE + 20):
            runtime.hub.publish(_elsewhere_op(org_admin.org_id, channel, seq=i + 1))
        reset = await sock.recv_until(lambda f: f["t"] == "reset")
        assert reset["reason"] == "overflow"
        assert [f for f in sock.skipped if f["t"] == "doc"] == [], (
            "the queue's contents are dropped for the marker, never delivered ahead of it"
        )
        # Reading the marker resumed delivery: what is published now arrives.
        runtime.hub.publish(_elsewhere_op(org_admin.org_id, channel, seq=9_000))
        assert (await sock.next_doc("op"))["seq"] == 9000
        # Still alive afterwards.
        await sock.send({"t": "ping"})
        assert (await sock.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"


async def test_a_graceful_shutdown_closes_an_open_socket_within_its_deadline(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A socket lives for its whole session, so a stop that waited for it would
    never finish. It does not have to: the transport closes the socket itself
    at the start of a graceful shutdown, with ``1012`` (service restart) — a
    code the client already backs off and re-mints a ticket on — and the
    process is then free to go, well inside the deadline the launch lines pass
    (which is what stops a socket that is somehow still running at it)."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with serve_controlled(graceful_timeout=2) as served:
        async with connect(served.addr, client) as sock:
            served.server.should_exit = True
            with pytest.raises(ConnectionClosed) as closed:
                while True:
                    # What is read here is the close itself; how long the shutdown
                    # took is asserted below, on the server task, not on this wait.
                    await sock.recv()
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1012
        try:
            await asyncio.wait_for(asyncio.shield(served.task), timeout=5.0)
        except TimeoutError:
            pytest.fail("the server was still waiting on connections 5s after a graceful stop")


async def test_a_platform_boxs_open_socket_is_closed_once_its_credential_is_revoked(
    uvicorn_server: str, real_session: AsyncSession, platform_admin: OrgWithAdmin
) -> None:
    """The socket is admitted as the machine once, and held for up to
    ``realtime_ws_max_session_seconds``. A revoke that only reached the REST
    doors would leave the box publishing into every chat it holds for that
    long. The tick re-asks the admission's question, so the box's socket is
    closed 4401 within one keepalive of the revoke — and the ticket it
    reconnects with is refused, which is what stops the daemon."""
    from alkera_core.auth.machine_token import machine_credential_headers
    from alkera_core.authz import agent_headers
    from alkera_core.compute.provider import EC2
    from tests._compute_helpers import make_machine_type
    from tests.conftest import mint_cli_token

    admin = await real_session.get(User, platform_admin.admin_id)
    assert admin is not None
    admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    machine_type = await make_machine_type(real_session, provider=EC2)
    console = await logged_in(platform_admin.admin_email, platform_admin.admin_password)
    minted = await console.post(
        "/admin/v1/machines",
        json={
            "label": "socket-box",
            "provider": machine_type.provider,
            "instance_type": machine_type.provider_type_id,
            "region": "us-west-2",
            "tenancy": "dedicated",
        },
    )
    assert minted.status_code == 201, minted.text
    token = await mint_cli_token(
        user_id=platform_admin.admin_id,
        email=platform_admin.admin_email,
        org_team_id=platform_admin.org_id,
    )
    box = AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    claimed = await box.post(
        "/api/v1/machines/claim",
        json={"provider_pod_id": f"i-{uuid4().hex[:12]}", "name": "b", "capacity": 4},
        headers={
            **agent_headers("booting"),
            **machine_credential_headers(minted.json()["credential"]),
        },
    )
    assert claimed.status_code == 201, claimed.text
    machine_id = str(claimed.json()["id"])
    owner, _ = await make_member(real_session, org_id=platform_admin.org_id, verified=True)
    chat, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="",
        client_id=None,
        machine_id=machine_id,
        machine_status="ready",
    )
    await real_session.commit()
    channel = f"doc:chat:{chat.id}"

    ticket = await _ticket_asserting(box, machine_id)
    async with connect(uvicorn_server, box, ticket=ticket) as the_box:
        assert await the_box.subscribe(channel) is True, "the box publishes the chat it serves"
        revoked = await console.delete(
            f"/admin/v1/machines/{minted.json()['machine']['credential_id']}"
        )
        assert revoked.status_code == 204
        assert await the_box.close_code() == close_codes.UNAUTHORIZED

    again = await box.post("/api/v1/ws/tickets", headers=agent_headers(machine_id))
    assert again.status_code == 401, again.text
    assert again.json()["error"]["code"] == "machine_credential_refused"
    await console.aclose()
    await box.aclose()


# ---------------------------------------------------------------------------
# The machine channel
# ---------------------------------------------------------------------------


def _promote_request(**overrides: Any) -> Any:
    """A ``machine.request`` as the promoter builds one."""
    from alkera_core.schemas.realtime import MachineRequest

    fields: dict[str, Any] = {
        "request_id": uuid4(),
        "kind": "promote",
        "lease_node_id": uuid4(),
        "epoch": 3,
        "node_id": uuid4(),
        "path": "sub/dir/file.csv",
        "expected": {"size": 1234, "mtime_ns": 1_758_625_000_123_456_789},
        "deadline_ms": 8000,
    }
    fields.update(overrides)
    return MachineRequest.model_validate(fields)


async def _publish_request(org_id: UUID, machine_id: str, request: Any) -> None:
    """Publish a request exactly as the promoter does: ``pg_notify`` in a
    transaction of its own, heard back through the served app's listener."""
    from alkera_core.files.promotion import machine_event, notify

    await notify(machine_event(org_id, machine_id, request))


async def test_a_machine_channel_admits_only_the_socket_that_proved_it_is_that_machine(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box holds its own channel; its operator's browser session, and the
    box naming a machine it is not, are refused — the same refusal whether the
    machine named exists or not, since the decision reads nothing."""
    machine_id, box = await _registered_machine(real_session, org_admin)
    other_machine, other_box = await _registered_machine(real_session, org_admin)
    browser = await logged_in(org_admin.admin_email, org_admin.admin_password)
    try:
        async with connect(
            uvicorn_server, box, ticket=await _ticket_asserting(box, machine_id)
        ) as the_box:
            await the_box.send({"t": "subscribe", "channel": f"machine:{machine_id}"})
            subscribed = await the_box.recv_until(lambda f: f["t"] in ("subscribed", "error"))
            assert (subscribed["t"], subscribed["channel"], subscribed["can_write"]) == (
                "subscribed",
                f"machine:{machine_id}",
                True,
            )
            # No roster follows: a machine channel has no presence.
            await the_box.send({"t": "ping"})
            assert (await the_box.recv())["t"] == "pong"
            for foreign in (other_machine, str(uuid4())):
                refused = await the_box.subscribe_error(f"machine:{foreign}")
                assert (refused["code"], refused["channel"]) == ("forbidden", f"machine:{foreign}")

        # The operator's own browser session naming the box's id on its ticket
        # is a person's socket: the assertion does not verify.
        async with connect(
            uvicorn_server, browser, ticket=await _ticket_asserting(browser, machine_id)
        ) as person:
            refused = await person.subscribe_error(f"machine:{machine_id}")
            assert refused["code"] == "forbidden"
        async with connect(uvicorn_server, browser) as person:
            refused = await person.subscribe_error(f"machine:{machine_id}")
            assert refused["code"] == "forbidden"
    finally:
        await box.aclose()
        await other_box.aclose()
        await browser.aclose()


async def test_a_machine_request_on_the_ephemeral_lane_reaches_the_subscribed_socket_as_its_frame(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A request published through ``pg_notify`` arrives at the machine's socket
    as the frame itself; a request for another machine, and one naming this
    machine under another org, never do."""
    machine_id, box = await _registered_machine(real_session, org_admin)
    other_machine, other_box = await _registered_machine(real_session, org_admin)
    try:
        async with connect(
            uvicorn_server, box, ticket=await _ticket_asserting(box, machine_id)
        ) as the_box:
            await the_box.send({"t": "subscribe", "channel": f"machine:{machine_id}"})
            assert (await the_box.recv_until(lambda f: f["t"] == "subscribed"))["can_write"]

            await _publish_request(org_admin.org_id, other_machine, _promote_request())
            await _publish_request(uuid4(), machine_id, _promote_request())
            request = _promote_request()
            await _publish_request(org_admin.org_id, machine_id, request)

            frame = await the_box.recv_until(lambda f: f["t"] == "machine.request")
            assert frame == json.loads(request.model_dump_json())
            await the_box.expect_nothing(1.0)
    finally:
        await box.aclose()
        await other_box.aclose()


async def test_a_machine_that_unsubscribes_stops_receiving_requests(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id, box = await _registered_machine(real_session, org_admin)
    try:
        async with connect(
            uvicorn_server, box, ticket=await _ticket_asserting(box, machine_id)
        ) as the_box:
            channel = f"machine:{machine_id}"
            await the_box.send({"t": "subscribe", "channel": channel})
            await the_box.recv_until(lambda f: f["t"] == "subscribed")
            await the_box.send({"t": "unsubscribe", "channel": channel})
            await the_box.send({"t": "ping"})
            await the_box.recv_until(lambda f: f["t"] == "pong")
            await _publish_request(org_admin.org_id, machine_id, _promote_request())
            await the_box.expect_nothing(1.0)
    finally:
        await box.aclose()


async def test_a_machine_ack_from_the_socket_reaches_the_hub_on_the_ephemeral_lane(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The ack is published under the socket's own verified machine and org —
    whatever it names — and arrives on this replica's hub through the listener,
    the way it reaches every other replica."""
    from alkera_core.files.promotion import MACHINE_ENTITY

    runtime = runtime_of(fastapi_app)
    assert runtime is not None
    heard = runtime.hub.subscribe(lambda event: event.type == "machine.ack", label="test-acks")
    machine_id, box = await _registered_machine(real_session, org_admin)
    request_id = str(uuid4())
    try:
        async with connect(
            uvicorn_server, box, ticket=await _ticket_asserting(box, machine_id)
        ) as the_box:
            await the_box.send(
                {
                    "t": "machine.ack",
                    "request_id": request_id,
                    "outcome": "changed",
                    "observed": {"size": 13, "mtime_ns": 42},
                }
            )
            event = await asyncio.wait_for(heard.queue.get(), HANG_BACKSTOP)
            assert isinstance(event, HubEvent)
            assert (event.lane, event.org_id, event.entity, event.entity_id, event.channel) == (
                "ephemeral",
                org_admin.org_id,
                MACHINE_ENTITY,
                machine_id,
                f"machine:{machine_id}",
            )
            assert event.payload == {
                "t": "machine.ack",
                "request_id": request_id,
                "outcome": "changed",
                "observed": {"size": 13, "mtime_ns": 42},
            }
            # The box's own ack is never echoed back to it.
            await the_box.expect_nothing(0.5)
    finally:
        runtime.hub.unsubscribe(heard)
        await box.aclose()


async def test_a_person_sending_a_machine_ack_is_refused_in_band_and_publishes_nothing(
    uvicorn_server: str, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    runtime = runtime_of(fastapi_app)
    assert runtime is not None
    heard = runtime.hub.subscribe(lambda event: event.type == "machine.ack", label="test-acks")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    try:
        async with connect(uvicorn_server, client) as person:
            await person.send({"t": "machine.ack", "request_id": str(uuid4()), "outcome": "busy"})
            refused = await person.recv_until(lambda f: f["t"] == "error")
            assert refused["code"] == "forbidden"
            await person.send({"t": "ping"})
            assert (await person.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
        assert heard.queue.empty()
    finally:
        runtime.hub.unsubscribe(heard)


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param({"t": "machine.hello"}, id="a-machine-tag-the-server-does-not-know"),
        pytest.param(
            {"t": "machine.ack", "request_id": "not-a-uuid", "outcome": "busy"},
            id="a-malformed-ack",
        ),
        pytest.param(
            {
                "t": "machine.ack",
                "request_id": "00000000-0000-4000-8000-000000000001",
                "outcome": "changed",
            },
            id="a-changed-ack-without-what-it-saw",
        ),
    ],
)
async def test_a_machine_speaking_outside_its_vocabulary_is_closed_as_a_protocol_error(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    frame: dict[str, Any],
) -> None:
    machine_id, box = await _registered_machine(real_session, org_admin)
    try:
        async with connect(
            uvicorn_server, box, ticket=await _ticket_asserting(box, machine_id)
        ) as the_box:
            await the_box.send(frame)
            assert await the_box.close_code() == close_codes.PROTOCOL_ERROR
    finally:
        await box.aclose()


# ---------------------------------------------------------------------------
# A box on its own machine credential
# ---------------------------------------------------------------------------


class _MachineBox:
    """A platform box on its credential: the client that speaks as it, the
    machine it holds, the credential behind it and the org it serves."""

    def __init__(self, box: Any, served_org: UUID) -> None:
        self.raw = box.raw
        self.credential_id: UUID = box.credential_id
        self.machine_id = str(box.machine_id)
        self.served_org = served_org
        self.client = AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {box.raw}"},
        )

    async def ticket(self) -> str:
        resp = await self.client.post("/api/v1/ws/tickets")
        assert resp.status_code == 200, resp.text
        return str(resp.json()["ticket"])

    async def aclose(self) -> None:
        await self.client.aclose()


async def _machine_box(
    operator: OrgWithAdmin, *, served_org: UUID | None, tenancy: str = "dedicated"
) -> _MachineBox:
    """A box holding a live machine on a credential minted in ``operator``'s
    org, dedicated to ``served_org`` (or a pool box serving every org), the
    way the console and ``/machines/claim`` leave one."""
    from tests.test_machine_principal_routes import _box

    box = await _box(operator, tenancy=tenancy, served_org=served_org)
    return _MachineBox(box, served_org if served_org is not None else operator.org_id)


async def _served_org() -> tuple[UUID, User, AsyncClient]:
    """A fresh org, a verified member of it and that member's browser."""
    tag = uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Served Org {tag}",
            admin_email=f"served-admin-{tag}@alkera.dev",
            admin_first_name="Served",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, password = await make_member(session, org_id=org.id, verified=True)
    assert password is not None
    return org.id, member, await logged_in(member.email, password)


async def _bound_chat(owner: User, *, machine_id: str | None) -> str:
    from tests.test_machine_principal_routes import _chat

    return await _chat(owner, machine_id=UUID(machine_id) if machine_id else None)


async def _rebind(chat_id: str, machine_id: str | None) -> None:
    """Move a chat onto ``machine_id`` (or off every machine) and ring its
    doorbell, as placement does."""
    from alkera_core.models import WorkspaceObject
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        if machine_id is None:
            await db.execute(
                text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                {"id": UUID(chat_id)},
            )
        else:
            await db.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": UUID(chat_id), "machine": machine_id},
            )
        chat = await db.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        await db.refresh(chat)
        await chat_service.announce_chat(db, chat=chat, actor=None)
        await db.commit()


async def _doc_op_rows(org_id: UUID, channel: str) -> list[Any]:
    from alkera_core.models import EventOutbox
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.DOC_OP.value,
                EventOutbox.entity_id == channel,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def test_a_box_mints_a_machine_ticket_and_a_person_a_persons(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The one mint route answers two shapes, decided by the credential and
    never by anything the caller says: a box on its machine credential gets a
    ticket bound to that credential and its machine; a person gets one bound
    to their session. A credential that stands behind no live machine —
    revoked, or never having claimed one — mints nothing, with the same
    refusal its every other door gives."""
    from alkera_core.auth import WsMachineTicket, WsTicket, decode_socket_ticket
    from alkera_core.models import MachineCredential
    from backend.services.credentials import machine_credentials as machine_credential_service
    from tests._compute_helpers import make_machine_type

    served_org, _owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    try:
        minted = decode_socket_ticket(await box.ticket())
        assert isinstance(minted, WsMachineTicket)
        assert (minted.credential_id, str(minted.machine_id), minted.org_id) == (
            box.credential_id,
            box.machine_id,
            org_admin.org_id,
        )
        resp = await browser.post("/api/v1/ws/tickets")
        assert resp.status_code == 200, resp.text
        person = decode_socket_ticket(resp.json()["ticket"])
        assert isinstance(person, WsTicket)
        assert person.org_id == served_org

        # A credential nobody has claimed a machine on.
        machine_type = await make_machine_type(real_session)
        _unclaimed, raw = await machine_credential_service.mint(
            real_session,
            org_id=org_admin.org_id,
            created_by=org_admin.admin_id,
            machine_type=machine_type,
            tenancy="pool",
            label="unclaimed",
        )
        await real_session.commit()
        refused = await box.client.post(
            "/api/v1/ws/tickets", headers={"Authorization": f"Bearer {raw}"}
        )
        assert refused.status_code == 401, refused.text
        assert refused.json()["error"]["code"] == "machine_credential_refused"

        # The box's own credential, revoked between the door and the mint.
        row = await real_session.get(MachineCredential, box.credential_id)
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        await real_session.commit()
        refused = await box.client.post("/api/v1/ws/tickets")
        assert refused.status_code == 401, refused.text
        assert refused.json()["error"]["code"] == "machine_credential_refused"
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_boxs_socket_holds_exactly_the_chats_bound_to_it(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Admitted on its machine ticket, the box subscribes the chat bound to it
    in the org it serves and publishes it: the hello hands it the document,
    its append is acked, lands on the outbox under the MACHINE's own actor
    chain, and reaches the chat's owner. A chat another box runs, a chat no
    box has taken, a chat bound to this very machine in an org the credential
    does not serve are each the opaque ``not_found``."""
    served_org, owner, browser = await _served_org()
    _foreign_org, stranger, stranger_browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    elsewhere = await _bound_chat(owner, machine_id=str(uuid4()))
    untaken = await _bound_chat(owner, machine_id=None)
    outside = await _bound_chat(stranger, machine_id=box.machine_id)
    channel = f"doc:chat:{mine}"
    try:
        async with (
            connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box,
            connect(uvicorn_server, browser) as reader,
        ):
            for refused in (
                f"doc:chat:{elsewhere}",
                f"doc:chat:{untaken}",
                f"doc:chat:{outside}",
            ):
                assert (await the_box.subscribe_error(refused))["code"] == "not_found", refused
            assert await the_box.subscribe(channel) is True, "the box publishes its chat"
            assert await reader.subscribe(channel) is True, "the owner reads their own chat"
            snapshot = await the_box.hello(channel)
            assert snapshot["epoch"] == 1
            assert (await reader.hello(channel))["payload"]["state"]["events"] == []
            ack = await the_box.op(
                channel,
                epoch=1,
                intent="append",
                op_id="op-box-1",
                events=[{"event_id": "e1", "event_type": "message.created"}],
            )
            assert ack["kind"] == "ack", ack
            assert ack["payload"]["seq"] == 1 and ack["payload"]["changed"] is True
            op = await reader.next_doc("op")
            assert op["peer_id"] == the_box.peer_id
            assert op["payload"]["events"][0]["event_id"] == "e1"
        rows = await _doc_op_rows(served_org, channel)
        assert rows, "the append is a durable row of the chat's org"
        assert [(link["kind"], link["id"]) for link in rows[-1].actor["chain"]] == [
            ("machine", box.machine_id)
        ]
        assert rows[-1].actor["delegating_user"] is None
    finally:
        await box.aclose()
        await browser.aclose()
        await stranger_browser.aclose()


async def test_a_pool_box_holds_a_bound_chat_in_any_org(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A pool box serves every org: a chat bound to it in an org it was never
    assigned to is its to publish, and a chat there bound elsewhere is not."""
    _org_id, owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=None, tenancy="pool")
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    elsewhere = await _bound_chat(owner, machine_id=str(uuid4()))
    try:
        async with connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box:
            assert await the_box.subscribe(f"doc:chat:{mine}") is True
            assert (await the_box.subscribe_error(f"doc:chat:{elsewhere}"))["code"] == "not_found"
            assert (await the_box.hello(f"doc:chat:{mine}"))["epoch"] == 1
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_boxs_socket_hears_no_frame_that_is_not_its_own(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """What reaches the box is the traffic of the channels it holds. An op on
    a chat of the served org it does not hold, the same op filed under an org
    it does not serve, a platform stream, a frame addressed to the chat's
    owner and a doorbell for somebody else's chat are published straight onto
    the served app's hub and none of them arrives; the owner's own op on the
    held chat does."""
    served_org, owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    elsewhere = await _bound_chat(owner, machine_id=str(uuid4()))
    runtime = runtime_of(fastapi_app)
    assert runtime is not None
    channel = f"doc:chat:{mine}"
    try:
        async with (
            connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box,
            connect(uvicorn_server, browser) as reader,
        ):
            assert await the_box.subscribe(channel) is True
            assert await reader.subscribe(channel) is True
            await the_box.hello(channel)
            await reader.hello(channel)
            runtime.hub.publish(_elsewhere_op(served_org, f"doc:chat:{elsewhere}", seq=1))
            runtime.hub.publish(_elsewhere_op(uuid4(), channel, seq=2))
            runtime.hub.publish(
                HubEvent(
                    lane="durable",
                    org_id=served_org,
                    type=EventType.CHAT_UPDATED.value,
                    entity="chat",
                    entity_id=mine,
                    version=1,
                    visibility="platform",
                    payload={},
                    id=3,
                )
            )
            runtime.hub.publish(
                HubEvent(
                    lane="durable",
                    org_id=served_org,
                    type=EventType.DOC_OP.value,
                    entity="doc",
                    entity_id=channel,
                    version=4,
                    visibility=f"user:{owner.id}",
                    payload=_elsewhere_op(served_org, channel, seq=4).payload,
                    id=4,
                    channel=channel,
                )
            )
            runtime.hub.publish(
                HubEvent(
                    lane="durable",
                    org_id=served_org,
                    type=EventType.CHAT_UPDATED.value,
                    entity="chat",
                    entity_id=elsewhere,
                    version=1,
                    visibility="org",
                    payload={"team_id": None, "machine_id": str(uuid4())},
                    id=5,
                )
            )
            await the_box.expect_nothing(1.0)
            ack = await reader.op(
                channel,
                epoch=1,
                intent="set_meta",
                op_id="op-owner-1",
                meta={"title": "Named by its owner"},
            )
            assert ack["kind"] == "ack", ack
            heard = await the_box.next_doc("op")
            assert heard["peer_id"] == reader.peer_id
            assert heard["payload"]["intent"] == "set_meta"
    finally:
        await box.aclose()
        await browser.aclose()


@pytest.mark.parametrize("how", ["revoke", "release"])
async def test_a_box_whose_standing_ends_is_cut_on_the_next_tick_and_mints_no_more(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin, how: str
) -> None:
    """The socket is admitted once and held for hours; the tick re-asks the
    credential's standing. A revoke, or the machine leaving the plane, closes
    the box's socket ``4401`` within one keepalive, and the ticket it would
    reconnect with is refused at the mint."""
    from alkera_core.models import MachineCredential
    from alkera_core.models.compute import ComputeAllocation

    served_org, owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    try:
        async with connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box:
            assert await the_box.subscribe(f"doc:chat:{mine}") is True
            async with AsyncSessionLocal() as db:
                if how == "revoke":
                    credential = await db.get(MachineCredential, box.credential_id)
                    assert credential is not None
                    credential.revoked_at = datetime.now(UTC)
                else:
                    machine = await db.get(ComputeAllocation, UUID(box.machine_id))
                    assert machine is not None
                    machine.state = "released"
                await db.commit()
            assert await the_box.close_code() == close_codes.UNAUTHORIZED
        refused = await box.client.post("/api/v1/ws/tickets")
        assert refused.status_code == 401, refused.text
        assert refused.json()["error"]["code"] == "machine_credential_refused"
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_machine_ticket_is_single_use_and_names_its_own_machine(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """One ticket opens one socket; the same ticket offered again is refused
    on every replica. A ticket that names the credential with a machine it
    does not hold, or a credential nobody minted, is refused at the handshake
    — the ticket says which machine, and the credential has to agree."""
    from alkera_core.auth import encode_ws_machine_ticket

    served_org, _owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    try:
        ticket = await box.ticket()
        async with connect(uvicorn_server, box.client, ticket=ticket) as first:
            async with connect(
                uvicorn_server, box.client, ticket=ticket, expect_welcome=False
            ) as replay:
                assert await replay.close_code() == close_codes.UNAUTHORIZED
            await first.send({"t": "ping"})
            assert (await first.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
        forged = encode_ws_machine_ticket(
            credential_id=box.credential_id, machine_id=uuid4(), org_id=org_admin.org_id
        )
        async with connect(
            uvicorn_server, box.client, ticket=forged, expect_welcome=False
        ) as other_machine:
            assert await other_machine.close_code() == close_codes.UNAUTHORIZED
        unknown = encode_ws_machine_ticket(
            credential_id=uuid4(), machine_id=UUID(box.machine_id), org_id=org_admin.org_id
        )
        async with connect(
            uvicorn_server, box.client, ticket=unknown, expect_welcome=False
        ) as nobody:
            assert await nobody.close_code() == close_codes.UNAUTHORIZED
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_box_has_no_presence_relays_nothing_and_signs_no_draft(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Everything a person does through the registry that is not the
    publisher's work is refused for a box, in band, and the socket stays up:
    a presence join, a caret and a relayed message (a person's, billed to the
    owner). A composer draft is refused as moved, for a box as for anyone: it
    lives on the live lane, where a box may not write. An ack naming a request
    this socket never forwarded goes nowhere."""
    served_org, owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    channel = f"doc:chat:{mine}"
    try:
        async with connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box:
            assert await the_box.subscribe(channel) is True
            await the_box.hello(channel)
            await the_box.send({"t": "presence.join", "channel": channel})
            joined = await the_box.recv_until(lambda f: f["t"] == "error")
            assert (joined["code"], joined["channel"]) == ("forbidden", channel)
            await the_box.send(
                {"t": "presence.cursor", "channel": channel, "cursor": {"offset": 0, "anchor": 0}}
            )
            assert (await the_box.recv_until(lambda f: f["t"] == "error"))["code"] == "forbidden"
            relayed = await the_box.op(
                channel,
                epoch=1,
                intent="user_message",
                events=[{"kind": "user_message", "text": "hi", "client_id": "c1"}],
            )
            assert (relayed["kind"], relayed["payload"]["code"]) == ("error", "forbidden")
            draft = await the_box.op(
                channel, epoch=1, intent="set_meta", meta={"draft": {"text": "x", "at": 7.0}}
            )
            assert (draft["kind"], draft["payload"]["code"]) == ("error", "draft_moved")
            await the_box.send({"t": "machine.ack", "request_id": str(uuid4()), "outcome": "busy"})
            unknown = await the_box.recv_until(lambda f: f["t"] == "error")
            assert unknown["code"] == "unknown_request"
            await the_box.send({"t": "ping"})
            assert (await the_box.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
        assert await _doc_op_rows(served_org, channel) == [], "nothing was written"
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_chat_rebound_away_from_the_box_is_dropped_on_its_doorbell(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The chat's doorbell is the one announcement that moves what a box
    holds. Rebound to another machine, the chat is dropped from the box's
    socket — the client told the ``not_found`` a fresh subscribe now answers,
    its next op ``not_subscribed`` — and the box may not hello it again. Bound
    back, the same doorbell lets the box hold it once more."""
    served_org, owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    mine = await _bound_chat(owner, machine_id=box.machine_id)
    channel = f"doc:chat:{mine}"
    try:
        async with connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box:
            assert await the_box.subscribe(channel) is True
            await the_box.hello(channel)
            await _rebind(mine, str(uuid4()))
            dropped = await the_box.recv_until(lambda f: f["t"] == "error")
            assert (dropped["code"], dropped["channel"]) == ("not_found", channel)
            refused = await the_box.op(channel, epoch=1, intent="append", events=[])
            assert (refused["kind"], refused["payload"]["code"]) == ("error", "not_subscribed")
            assert (await the_box.subscribe_error(channel))["code"] == "not_found"
            await _rebind(mine, box.machine_id)
            assert await the_box.subscribe(channel) is True
            assert (await the_box.hello(channel))["epoch"] == 1
    finally:
        await box.aclose()
        await browser.aclose()


async def test_a_boxs_ack_is_published_under_the_org_of_the_request_it_answers(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The drive that asked listens under the CHAT's org, which for a platform
    box is not the box's own. The request reaches the box on its machine
    channel; its ack goes back under the org the request came from."""
    from alkera_core.files.promotion import MACHINE_ENTITY

    served_org, _owner, browser = await _served_org()
    box = await _machine_box(org_admin, served_org=served_org)
    runtime = runtime_of(fastapi_app)
    assert runtime is not None
    heard = runtime.hub.subscribe(lambda event: event.type == "machine.ack", label="test-acks")
    try:
        async with connect(uvicorn_server, box.client, ticket=await box.ticket()) as the_box:
            await the_box.send({"t": "subscribe", "channel": f"machine:{box.machine_id}"})
            assert (await the_box.recv_until(lambda f: f["t"] == "subscribed"))["can_write"]
            assert (await the_box.subscribe_error(f"machine:{uuid4()}"))["code"] == "forbidden"
            request = _promote_request()
            await _publish_request(served_org, box.machine_id, request)
            frame = await the_box.recv_until(lambda f: f["t"] == "machine.request")
            assert frame == json.loads(request.model_dump_json())
            await the_box.send(
                {"t": "machine.ack", "request_id": str(request.request_id), "outcome": "accepted"}
            )
            event = await asyncio.wait_for(heard.queue.get(), HANG_BACKSTOP)
            assert isinstance(event, HubEvent)
            assert (event.org_id, event.entity, event.entity_id, event.channel) == (
                served_org,
                MACHINE_ENTITY,
                box.machine_id,
                f"machine:{box.machine_id}",
            )
            assert event.payload["request_id"] == str(request.request_id)
            await the_box.expect_nothing(0.5)
    finally:
        runtime.hub.unsubscribe(heard)
        await box.aclose()
        await browser.aclose()
