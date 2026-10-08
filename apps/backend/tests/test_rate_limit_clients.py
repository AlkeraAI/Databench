"""Realistic clients under the shipped numbers: none of them ever sees a 429.

Every case here is a guard against sizing a class so tightly it breaks the
product — a folder drop, a fast conversation, a reconnecting tab, a busy box, a
chatty Slack workspace, a socket streaming frames. Each drives the real
enforcer with the real class numbers on a clock only the test moves, at the
worst realistic cadence (a same-instant burst where a client would produce
one), and then proves the budget is still a budget by crossing it on purpose.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from alkera_core.auth import encode_cli_token
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from backend.api.rate_limit import REGISTRY, enforce_rate_limit, limited
from backend.services.realtime import session as socket_session
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from tests.conftest import ManualClock, OrgWithAdmin, login
from tests.test_ws_gateway import connect

pytestmark = pytest.mark.asyncio

Send = Callable[[], Awaitable[Response]]


def _probe_app() -> FastAPI:
    """The product's route families under the real enforcer, one route each."""
    probe = FastAPI(dependencies=[Depends(enforce_rate_limit)])
    upload = [Depends(limited("upload"))]
    part = [Depends(limited("upload_part"))]
    chat = [Depends(limited("chat"))]
    stream = [Depends(limited("stream_open"))]
    machine = [Depends(limited("machine"))]
    webhook = [Depends(limited("webhook"))]

    @probe.post("/uploads", dependencies=upload, status_code=201)
    async def open_upload() -> dict[str, str]:
        return {"session": secrets.token_hex(4)}

    @probe.put("/uploads/{session}/parts/{part_no}", dependencies=part)
    async def put_part(session: str, part_no: int) -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/uploads/{session}/complete", dependencies=part)
    async def complete(session: str) -> dict[str, bool]:
        return {"ok": True}

    # The finish of a drop: one poll per commit, the folder's listing, and the
    # committed nodes read back a hundred at a time -- or, the shape this
    # replaced, one item read per file.
    @probe.get("/drives/{drive}/operations/{op}", dependencies=[Depends(limited("operation"))])
    async def poll(drive: str, op: str) -> dict[str, str]:
        return {"state": "done"}

    @probe.post("/drives/{drive}/items/lookup", dependencies=[Depends(limited("read"))])
    async def lookup(drive: str) -> dict[str, list[str]]:
        return {"value": []}

    @probe.get("/drives/{drive}/items/{item}/children")
    async def children(drive: str, item: str) -> dict[str, list[str]]:
        return {"value": []}

    @probe.get("/drives/{drive}/items/{item}")
    async def item(drive: str, item: str) -> dict[str, str]:
        return {"etag": "1"}

    @probe.post("/chats/{chat_id}/messages", dependencies=chat, status_code=201)
    async def prompt(chat_id: str) -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/chats/{chat_id}/answer", dependencies=chat, status_code=202)
    async def answer(chat_id: str) -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/chats/{chat_id}/messages", dependencies=chat)
    async def read_messages(chat_id: str) -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/events", dependencies=stream)
    async def open_stream() -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/machines/{machine_id}/heartbeat", dependencies=machine, status_code=204)
    async def heartbeat(machine_id: str) -> None:
        return None

    @probe.post("/push", dependencies=machine)
    async def push() -> dict[str, bool]:
        return {"ok": True}

    @probe.post("/slack/events", dependencies=webhook)
    async def slack_events() -> dict[str, bool]:
        return {"ok": True}

    @probe.get("/owner")
    async def owner_read() -> dict[str, bool]:
        return {"ok": True}

    REGISTRY.install(probe)
    return probe


def _jwt() -> str:
    token, _ = encode_cli_token(
        user_id=uuid4(),
        email=f"{secrets.token_hex(4)}@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
    )
    return token


@pytest.fixture
def probe(pin_clock: ManualClock) -> AsyncClient:
    del pin_clock
    return AsyncClient(transport=ASGITransport(app=_probe_app()), base_url="http://probe")


async def _in_flight(sends: list[Send], *, at_once: int) -> list[int]:
    """Run ``sends`` with at most ``at_once`` outstanding, the way a client does."""
    gate = asyncio.Semaphore(at_once)

    async def one(send: Send) -> int:
        async with gate:
            return (await send()).status_code

    return list(await asyncio.gather(*(one(send) for send in sends)))


async def test_a_500_file_folder_drop_at_three_in_flight_finishes_with_no_refusal(
    probe: AsyncClient,
) -> None:
    """Each file is an open, a part and a complete. The clock does not move for
    the whole drop — the worst case, since a real drop takes a few seconds of
    refill — so the burst alone has to hold it."""
    user = {"Authorization": f"Bearer {_jwt()}"}

    async def upload() -> Response:
        opened = await probe.post("/uploads", headers=user)
        if opened.status_code != 201:
            return opened
        session = opened.json()["session"]
        put = await probe.put(f"/uploads/{session}/parts/1", headers=user)
        if put.status_code != 200:
            return put
        return await probe.post(f"/uploads/{session}/complete", headers=user)

    statuses = await _in_flight([upload] * 500, at_once=3)
    assert statuses.count(429) == 0, f"{statuses.count(429)} of 500 uploads were refused"
    assert all(s == 200 for s in statuses)
    # Still a budget: a script opening sessions far past any folder drop is stopped.
    burst = settings.rate_limit_upload_burst
    flood = [(await probe.post("/uploads", headers=user)).status_code for _ in range(burst)]
    assert 429 in flood


async def test_a_500_file_drop_finishes_its_reads_in_batches_with_no_refusal(
    probe: AsyncClient,
) -> None:
    """The whole finish of a drop, clock still: every commit polled once, the
    folder listing refreshed once per quarter second of landing files, and the
    committed nodes read back a hundred at a time. Then the shape it replaced
    -- one item read per finished file -- against the same read budget, which
    refuses it: the batching is what keeps the drop under budget, not slack."""
    user = {"Authorization": f"Bearer {_jwt()}"}

    async def upload() -> Response:
        opened = await probe.post("/uploads", headers=user)
        if opened.status_code != 201:
            return opened
        session = opened.json()["session"]
        for step in (
            lambda: probe.put(f"/uploads/{session}/parts/1", headers=user),
            lambda: probe.post(f"/uploads/{session}/complete", headers=user),
            lambda: probe.get(f"/drives/d/operations/op-{session}", headers=user),
        ):
            answered = await step()
            if answered.status_code != 200:
                return answered
        return answered

    statuses = await _in_flight([upload] * 500, at_once=3)
    assert statuses.count(429) == 0, f"{statuses.count(429)} of 500 uploads were refused"
    # A 500-file drop lands over a few seconds; the listing is refetched once
    # per quarter second of it, twenty times at the outside.
    listings = [
        (await probe.get("/drives/d/items/folder/children", headers=user)).status_code
        for _ in range(20)
    ]
    lookups = [
        (await probe.post("/drives/d/items/lookup", headers=user)).status_code for _ in range(5)
    ]
    assert listings == [200] * 20
    assert lookups == [200] * 5

    # The old finish, on a fresh person: an item read per file, the tail refused.
    other = {"Authorization": f"Bearer {_jwt()}"}
    per_file = [
        (await probe.get(f"/drives/d/items/node-{n}", headers=other)).status_code
        for n in range(500)
    ]
    assert 429 in per_file, "an item read per file fits the read budget; the batching is slack"
    assert per_file.index(429) >= settings.rate_limit_read_burst


async def test_a_fast_conversation_never_sees_a_refusal(
    probe: AsyncClient, pin_clock: ManualClock
) -> None:
    """Two hundred prompt / answer / permission calls in a minute, interleaved
    with the webview reading the thread back: a person typing as fast as they
    can, or a Slack thread with several people in it."""
    user = {"Authorization": f"Bearer {_jwt()}"}
    calls = [
        lambda: probe.post("/chats/c1/messages", headers=user),
        lambda: probe.post("/chats/c1/answer", headers=user),
        lambda: probe.get("/chats/c1/messages", headers=user),
    ]
    statuses = []
    for i in range(200):
        pin_clock.advance(0.3)
        statuses.append((await calls[i % 3]()).status_code)
    assert 429 not in statuses
    # A same-instant burst — a webview replaying its queue after a reconnect a
    # second later — has the whole burst to spend.
    pin_clock.advance(1.0)
    burst = settings.rate_limit_chat_burst
    same_instant = [
        (await probe.get("/chats/c1/messages", headers=user)).status_code for _ in range(burst)
    ]
    assert 429 not in same_instant


async def test_twenty_reconnects_in_a_minute_at_the_client_backoff_floor_see_none(
    probe: AsyncClient, pin_clock: ManualClock
) -> None:
    """The browser's stream reconnects with a two-second floor; a flapping
    network can reopen it twenty times in a minute and every open is admitted.
    Only a same-instant storm past the burst — no client backs off that little —
    is refused, and the refusal names the wait."""
    user = {"Authorization": f"Bearer {_jwt()}"}
    statuses = []
    for _ in range(20):
        pin_clock.advance(2.0)
        statuses.append((await probe.get("/events", headers=user)).status_code)
    assert statuses == [200] * 20
    burst = settings.rate_limit_stream_open_burst
    storm = [(await probe.get("/events", headers=user)).status_code for _ in range(burst + 1)]
    assert storm[-1] == 429


async def test_a_busy_box_never_starves_its_owner(
    probe: AsyncClient, pin_clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box heartbeats every five seconds and pushes a 300-node delta at
    three in flight, all inside one minute. None of it is refused — and none
    of it touches the owner's own budget, which is pinned to one request here
    so the very next browser call would show a shared bucket."""
    owner = {"Authorization": f"Bearer {_jwt()}"}
    box = {**owner, **agent_headers("sess-box-1")}
    statuses = []
    for _ in range(12):
        pin_clock.advance(5.0)
        statuses.append((await probe.post("/machines/m1/heartbeat", headers=box)).status_code)
    push = [lambda: probe.post("/push", headers=box)] * 300
    statuses.extend(await _in_flight(push, at_once=3))
    assert 429 not in statuses, f"{statuses.count(429)} box calls refused"
    monkeypatch.setattr(settings, "rate_limit_read_burst", 1)
    monkeypatch.setattr(settings, "rate_limit_read_per_minute", 1)
    assert (await probe.get("/owner", headers=owner)).status_code == 200


async def test_a_slack_workspace_delivering_100_events_in_a_minute_is_accepted(
    probe: AsyncClient, pin_clock: ManualClock
) -> None:
    """A signed delivery refused with a 429 is retried by Slack into a storm;
    the class is sized to accept and drain instead — a busy workspace at one
    event every 600 ms, and the same hundred landing at once."""
    body = {"team_id": "T0WORKSPACE", "event": {"type": "app_mention"}}
    paced = []
    for _ in range(100):
        pin_clock.advance(0.6)
        paced.append((await probe.post("/slack/events", json=body)).status_code)
    assert paced == [200] * 100
    at_once = [(await probe.post("/slack/events", json=body)).status_code for _ in range(100)]
    assert at_once == [200] * 100


async def test_a_thousand_socket_frames_never_touch_a_bucket(
    uvicorn_server: str,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    pin_clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket is charged once, at the handshake. The socket's own per-
    connection frame meter is a different mechanism (a flood on ONE socket);
    it is opened wide here so a thousand frames reach the server, and the
    registry's ``stream_open`` budget for the user has still spent exactly the
    one open."""
    del pin_clock
    monkeypatch.setattr(socket_session._FrameRate, "allow", lambda self, now: True)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    key = f"user:{org_admin.admin_id}"
    limiter = REGISTRY.limiter("stream_open")
    async with connect(uvicorn_server, client) as sock:
        for _ in range(1_000):
            await sock.ws.send(json.dumps({"t": "ping"}))
        pongs = 0
        while pongs < 1_000:
            frame = json.loads(await asyncio.wait_for(sock.ws.recv(), timeout=8.0))
            if frame["t"] == "pong":
                pongs += 1
        # One open charged; this probe is the second spend on the key.
        after = limiter.consume(key, now=REGISTRY.clock())
        assert after.allowed
        assert after.remaining == settings.rate_limit_stream_open_burst - 2


async def test_an_office_signing_in_together_behind_one_address_sees_no_refusal(
    client: AsyncClient, pin_clock: ManualClock
) -> None:
    """Ten editors behind one NAT start a device login in the same instant and
    each polls at the issued interval for two minutes while people find the
    browser tab. One address, ten codes: neither leg refuses any of it — and
    an eleventh poller hammering with no pause is still stopped."""
    started = [
        await client.post("/api/v1/auth/device/code", data={"client_id": "alkera-vscode"})
        for _ in range(10)
    ]
    assert [r.status_code for r in started] == [200] * 10
    devices = [r.json() for r in started]
    interval = devices[0]["interval"]

    def form(device_code: str) -> dict[str, str]:
        return {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": "alkera-vscode",
        }

    refused = 0
    for _ in range(120 // interval):
        pin_clock.advance(interval)
        for device in devices:
            polled = await client.post(
                "/api/v1/auth/device/token", data=form(device["device_code"])
            )
            refused += polled.status_code == 429
    assert refused == 0

    hammer = form(secrets.token_urlsafe(32))
    statuses = [
        (await client.post("/api/v1/auth/device/token", data=hammer)).status_code
        for _ in range(settings.rate_limit_device_poll_burst + 1)
    ]
    assert statuses[-1] == 429
