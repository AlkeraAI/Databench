"""``GET /api/v1/events`` over real TCP: a uvicorn thread with the lifespan
on and running on the test loop, a browser-shaped client reading frames as
they arrive.

What only a live connection can prove: a committed row reaches an open stream
through the server's own listener; a client that disconnects frees its slot;
a reconnect with ``Last-Event-ID`` or ``?after=`` replays exactly the gap; a
revocation ends the stream at the next keepalive; the lifespan starts and
stops the listener. Events are injected THROUGH THE DATABASE (``emit`` +
commit), the way every producer in the product injects them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.auth import encode_session_token, register_token, revoke_all_for_user
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, emit
from alkera_core.models import TokenType
from backend.services.realtime import sse
from backend.services.realtime.limits import ConnectionGate
from backend.services.realtime.runtime import runtime_of
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, serve_app, serve_controlled

STREAM = "/api/v1/events"
READ_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def _fast_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server thread reads the shared settings object, so a re-auth tick
    lands within a second instead of fifteen."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(addr: str, email: str, password: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=f"http://{addr}", timeout=httpx.Timeout(READ_TIMEOUT))
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return client


async def _emit(
    org_id: UUID,
    *,
    entity_id: str | None = None,
    visibility: str = "org",
    payload: dict[str, Any] | None = None,
    version: int = 1,
) -> int:
    async with AsyncSessionLocal() as session:
        row = await emit(
            session,
            org_id=org_id,
            type=EventType.KB_ITEM_CHANGED,
            entity="kb_item",
            entity_id=entity_id or uuid4().hex,
            version=version,
            visibility=visibility,
            payload=payload,
        )
        await session.commit()
        return int(row.id)


class _Stream:
    """One open event stream, read frame by frame as the server writes it."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self._lines = response.aiter_lines()
        self.frames: list[dict[str, str]] = []

    async def next_frame(self, seconds: float = READ_TIMEOUT) -> dict[str, str]:
        """The next complete frame (a blank line ends one)."""
        frame: dict[str, str] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no frame within {seconds}s; partial={frame}")
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

    async def next_event(self, seconds: float = READ_TIMEOUT) -> dict[str, str]:
        """Skip comments and control frames; return the next named event."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            frame = await self.next_frame(max(0.01, deadline - loop.time()))
            if "event" in frame:
                return frame

    async def expect_connected(self) -> None:
        assert (await self.next_frame())["retry"] == "2000"
        assert (await self.next_frame())["comment"] == "connected"


@contextlib.asynccontextmanager
async def _open(
    client: httpx.AsyncClient, *, headers: dict[str, str] | None = None, params: dict | None = None
) -> AsyncIterator[_Stream]:
    async with client.stream("GET", STREAM, headers=headers, params=params) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        yield _Stream(response)


async def _until(predicate: Callable[[], bool], seconds: float = READ_TIMEOUT) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _data(frame: dict[str, str]) -> dict[str, Any]:
    return dict(json.loads(frame["data"]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_frames_arrive_live_after_a_committed_emit(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    client = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(client), _open(client) as stream:
        await stream.expect_connected()
        row_id = await _emit(org_admin.org_id, entity_id="live", version=7)
        frame = await stream.next_event()
    assert frame["id"] == str(row_id)
    assert frame["event"] == "kb_item.changed"
    assert _data(frame) == {
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "live",
        "version": 7,
        "org_id": str(org_admin.org_id),
    }


async def test_rows_for_another_org_never_reach_the_stream(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    client = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(client), _open(client) as stream:
        await stream.expect_connected()
        await _emit(uuid4(), entity_id="theirs")
        mine = await _emit(org_admin.org_id, entity_id="mine")
        frame = await stream.next_event()
    assert frame["id"] == str(mine)
    assert all("theirs" not in f.get("data", "") for f in stream.frames)


async def test_client_disconnect_releases_the_slot_and_the_subscription(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    runtime = runtime_of(fastapi_app)
    assert runtime is not None
    gate = ConnectionGate.sse()
    # The runtime's own consumers (the transcript watch) hold subscriptions of
    # their own; the stream's is the one on top of them.
    base = runtime.hub.subscriber_count
    c = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(c):
        async with _open(c) as stream:
            await stream.expect_connected()
            assert gate.active == 1
            assert runtime.hub.subscriber_count == base + 1
        # The response context closed the connection; the server notices the
        # disconnect and tears the generator down.
        await _until(lambda: gate.active == 0)
        await _until(lambda: runtime.hub.subscriber_count == base)


@pytest.mark.parametrize("carrier", ["header", "query"])
async def test_reconnect_replays_exactly_the_gap_and_nothing_twice(
    uvicorn_server: str, org_admin: OrgWithAdmin, carrier: str
) -> None:
    c = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(c):
        async with _open(c) as first:
            await first.expect_connected()
            seen = await _emit(org_admin.org_id, entity_id="seen")
            assert (await first.next_event())["id"] == str(seen)
        # Disconnected. Rows land while nobody is listening.
        missed_1 = await _emit(org_admin.org_id, entity_id="missed-1")
        await _emit(uuid4(), entity_id="theirs")
        missed_2 = await _emit(org_admin.org_id, entity_id="missed-2")
        headers = {"Last-Event-ID": str(seen)} if carrier == "header" else None
        params = {"after": str(seen)} if carrier == "query" else None
        async with _open(c, headers=headers, params=params) as second:
            await second.expect_connected()
            replayed = [await second.next_event(), await second.next_event()]
            # Then live delivery continues from where the replay ended.
            live = await _emit(org_admin.org_id, entity_id="live")
            after = await second.next_event()
    assert [f["id"] for f in replayed] == [str(missed_1), str(missed_2)]
    assert after["id"] == str(live)
    ids = [f["id"] for f in second.frames if "id" in f]
    assert len(ids) == len(set(ids)), "no frame is delivered twice across a resume"
    assert str(seen) not in ids


async def test_keepalive_re_auth_ends_a_revoked_session(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    client = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(client), _open(client) as stream:
        await stream.expect_connected()
        async with AsyncSessionLocal() as db:
            await revoke_all_for_user(db, org_admin.admin_id)
            await db.commit()
        error = await stream.next_event(seconds=5.0)
        assert error["event"] == "error"
        assert _data(error) == {"code": "unauthorized"}
        # The server closes after the error frame: the line iterator ends.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream._lines.__anext__(), timeout=5.0)
        # And the reconnect is refused.
        assert (await client.get(STREAM)).status_code == 401


async def test_a_query_string_token_is_refused_over_tcp(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    token, claims = encode_session_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
    )
    async with AsyncSessionLocal() as db:
        await register_token(db, claims=claims, token_type=TokenType.SESSION)
        await db.commit()
    async with httpx.AsyncClient(base_url=f"http://{uvicorn_server}", timeout=5.0) as anon:
        assert (await anon.get(STREAM, params={"token": token})).status_code == 401
        async with anon.stream(
            "GET", STREAM, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            assert response.status_code == 200


async def test_a_burst_larger_than_the_queue_resets_the_subscriber(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    """One transaction with more rows than a subscriber's queue holds: the
    listener publishes them all before the reader can drain, the queue
    overflows, and the client gets a ``reset`` instead of a partial history."""
    from alkera_core.events.hub import DEFAULT_QUEUE_MAXSIZE

    client = await _login(uvicorn_server, org_admin.admin_email, org_admin.admin_password)
    async with aclosing(client), _open(client) as stream:
        await stream.expect_connected()
        async with AsyncSessionLocal() as session:
            for i in range(DEFAULT_QUEUE_MAXSIZE + 20):
                await emit(
                    session,
                    org_id=org_admin.org_id,
                    type=EventType.KB_ITEM_CHANGED,
                    entity="kb_item",
                    entity_id=f"burst-{i}",
                )
            await session.commit()
        first = await stream.next_event()
        assert first["event"] == "reset"
        assert _data(first) == {"reason": "overflow"}
        # Delivery resumes once the reset is acknowledged.
        tail = await _emit(org_admin.org_id, entity_id="after-burst")
        frame = await stream.next_event()
        while frame.get("id") != str(tail):
            frame = await stream.next_event()


async def test_a_graceful_shutdown_ends_an_open_stream_within_its_deadline(
    org_admin: OrgWithAdmin,
) -> None:
    """A graceful stop closes the listening socket and then waits for the open
    connections. This stream is open for up to fifty minutes, so with no
    deadline the process never goes away — the reloader's replacement never
    binds, and every later request sits in the accept backlog. The deadline the
    launch lines pass cancels the connection, and the stream reads that cancel
    the way it reads its own: a last ``retry:`` and a clean end of body, so the
    client reconnects with its cursor instead of seeing the response truncated.
    """
    async with serve_controlled(graceful_timeout=2) as served:
        client = await _login(served.addr, org_admin.admin_email, org_admin.admin_password)
        async with aclosing(client), _open(client) as stream:
            await stream.expect_connected()
            served.server.should_exit = True
            frames: list[dict[str, str]] = []
            with pytest.raises(StopAsyncIteration):
                while True:  # read to the end of the body, however it ends
                    frames.append(await stream.next_frame(seconds=8.0))
            assert frames[-1] == {"retry": str(sse.RETRY_AFTER_DEADLINE_MS)}
        try:
            await asyncio.wait_for(asyncio.shield(served.task), timeout=5.0)
        except TimeoutError:
            pytest.fail("the server was still waiting on connections 5s after a graceful stop")


async def test_the_lifespan_starts_and_stops_the_listener() -> None:
    assert runtime_of(fastapi_app) is None
    async with serve_app() as addr:
        runtime = runtime_of(fastapi_app)
        assert runtime is not None
        assert runtime.listener is not None
        assert await runtime.listener.wait_connected(5.0)
        async with httpx.AsyncClient(base_url=f"http://{addr}", timeout=5.0) as anon:
            assert (await anon.get("/health/live")).status_code == 200
    # Shutdown ran: the listener is stopped and the runtime unbound.
    assert runtime.listener.connected is False
    assert runtime.listener.running is False
    assert runtime_of(fastapi_app) is None
    # A second server on the same app object starts clean.
    async with serve_app():
        again = runtime_of(fastapi_app)
        assert again is not None and again is not runtime
