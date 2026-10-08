"""``GET /api/v1/events`` in process: authentication, filtering, cursors,
keepalive re-auth, resets, caps and the OpenAPI contract.

httpx's ``ASGITransport`` collects a whole response body before returning, so
a live stream cannot be read through it. A case whose subject IS the stream's
own bound — it closes at its deadline, it ticks four keepalives, a revocation
ends it at the next tick — therefore bounds the stream through ``settings`` and
asserts on the collected text once the server closed it.

A case that has to see a frame ARRIVE does not: a window short enough to
collect a finished body would be the synchronisation, and on a loaded box the
path from a commit to a frame outlasts one. Those read the stream as the server
writes it (``tests._sse_reader``) and wait for the frame instead, closing by
disconnect. Anything that needs the real socket (a disconnect a kernel reports,
a resume across a new connection) is in ``test_events_sse_live.py`` over TCP.

The app's lifespan runs on the test loop (``realtime_app``), so the runtime's
hub and a real outbox listener exist; a test publishes straight to the hub or
commits through the outbox, and both reach the stream on the same loop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import aclosing, asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import encode_session_token, register_token, revoke_all_for_user
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventHub, EventType, HubEvent, emit, latest_id
from alkera_core.models import TeamRole, TokenType, User
from backend.api.routes.realtime import events as events_route
from backend.authz import decide_on_record
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from backend.services.realtime import runtime as realtime_runtime
from backend.services.realtime.filters import EntitlementRef
from backend.services.realtime.limits import ConnectionGate
from backend.services.realtime.runtime import runtime_of
from fastapi import FastAPI
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests._live_window import live_window
from tests._sse_reader import open_event_stream
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login, make_member

#: Every case builds its own org, its own users and its own stream, and the
#: process-wide hub and connection gates are cleared on both sides of each one
#: (``_reset_realtime_state``), so nothing here reads what a neighbour left
#: behind and the cases are free to land on different workers.
pytestmark = [pytest.mark.spread]

STREAM = "/api/v1/events"
#: Every wall-clock number here carries the run's allowance. They are not
#: claims — no case here says "a stream lasts two hundred milliseconds"; they
#: are the bound that makes a live stream readable through ``ASGITransport``,
#: and everything a case does has to fit inside one. Held to the published
#: number, that bound became the assertion on a shared runner: sixty-four
#: workers on one Postgres park a commit, or the loop itself, for longer than
#: the whole stream, and nine cases here went red for the machine's load with
#: nothing about the stream changed. Scaled, the bound stays what it is for —
#: a stream that ends on its own — and stops deciding the gate.
#:
#: A stream ALWAYS runs to its bound: nothing ends it early but a session that
#: stopped being valid, so whatever a case names is what the case costs. One
#: number for every stream therefore charges the whole file what the slowest
#: case needs, which is how a module of small claims came to take two and a
#: half minutes. So the bound is picked from what has to happen INSIDE the
#: stream, and the ladder below runs from nothing at all to a committed row
#: travelling ``NOTIFY`` back through the listener.
#:
#: One keepalive tick. Every tick costs the database four round trips, so this
#: is not driven to zero: a stream bounded at a few of these is cheap, and a
#: stream whose interval is below a re-check's own latency is a spin loop that
#: starves the delivery the cases are about.
KEEPALIVE = live_window(0.1).seconds
#: The DEFAULT bound, and the cheapest one: a stream that carries nothing which
#: has to ARRIVE while it is open. Its whole body — the opening frames and the
#: catch-up replay — is written before the live loop runs its first iteration,
#: so the only thing this has to outlast is a keepalive tick, and it is exactly
#: two of them: a case that reads ": keepalive" off such a body always finds
#: one, on any runner, because both numbers carry the same allowance. Most
#: cases here assert on a replay; the ones that need something to arrive say so
#: below and name the bound that covers the arrival.
DEADLINE = live_window(0.2).seconds
#: What a stream is bounded by when the case publishes STRAIGHT TO THE HUB
#: while it is open. That publish is in-process and lands microseconds after
#: the subscribe edge the case waited for, so this covers the scheduling delay
#: between the two on a loaded runner — no I/O at all.
LIVE_STREAM_SECONDS = live_window(0.4).seconds
#: What a stream is bounded by when the case WRITES to the database inside it
#: (a revocation, a deactivation, a backdated idle window) and reads what the
#: next tick made of that write. A write is a session checkout and a commit on
#: the one Postgres the run shares; it is not bounded by the keepalive rhythm.
WRITE_STREAM_SECONDS = live_window(0.8).seconds
WAIT = live_window(5.0).seconds
#: The mid-stream re-entitlement case waits for a keepalive tick it can observe
#: rather than for a number of seconds, so its stream has to outlive that wait:
#: the bound is what fails the test, and it fails naming the tick that never came.
REFRESH_WAIT_SECONDS = live_window(1.0).seconds
REFRESH_STREAM_SECONDS = live_window(1.5).seconds
#: A case that commits a REAL outbox row inside its stream has to outlive that
#: commit, and a commit is not bounded by the keepalive rhythm the default
#: stream is sized for: it queues behind whatever else holds the one Postgres
#: the run shares. Held to the default bound, those cases were asserting how
#: busy the database was — a parked commit landed after the stream had already
#: closed and the frame the case is about was never written. This bound is the
#: backstop for the commit, not the claim; each such case still fails on the
#: frames it read.
COMMIT_STREAM_SECONDS = live_window(3.0).seconds
#: What a cap case's stream is bounded by: nothing a run can reach. A cap is a
#: claim about what happens while a stream is OPEN, so the stream behind the
#: refusal has to outlive every request the case makes — see ``_held_stream``,
#: which ends it by dropping the client instead of waiting this out.
HELD_STREAM_SECONDS = 3600.0


@pytest.fixture(autouse=True)
def _short_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stream here ends on its own, at the cheapest bound in the ladder:
    the one for a stream that has to carry nothing arriving while it is open.
    It keeps the same two keepalive ticks inside it whatever allowance the run
    carries — a case reading a tick off the body reads one on a quiet machine
    and on a loaded one — and a case that needs more than a replay raises the
    bound itself, naming what has to arrive."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", DEADLINE)
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", KEEPALIVE)


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frames(body: str) -> list[dict[str, str]]:
    """Parse ``text/event-stream`` text into one dict per non-comment frame
    (``id`` / ``event`` / ``data`` / ``retry`` keys, as present)."""
    out: list[dict[str, str]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame.setdefault("comment", line[1:].strip())
                continue
            key, _, value = line.partition(":")
            frame[key] = value.strip()
        out.append(frame)
    return out


def _events(body: str) -> list[dict[str, str]]:
    return [f for f in _frames(body) if "event" in f and f["event"] not in ("reset", "error")]


def _named(body: str, name: str) -> list[dict[str, str]]:
    return [f for f in _frames(body) if f.get("event") == name]


def _comments(body: str) -> list[str]:
    return [f["comment"] for f in _frames(body) if "comment" in f]


async def _emit(
    org_id: UUID,
    *,
    type: EventType = EventType.KB_ITEM_CHANGED,
    entity_id: str | None = None,
    visibility: str = "org",
    payload: dict[str, Any] | None = None,
    version: int = 1,
) -> int:
    async with AsyncSessionLocal() as session:
        row = await emit(
            session,
            org_id=org_id,
            type=type,
            entity="kb_item",
            entity_id=entity_id or uuid4().hex,
            version=version,
            visibility=visibility,
            payload=payload,
        )
        await session.commit()
        return int(row.id)


async def _head() -> int:
    """The outbox id a stream opening now stands at: with no cursor the route
    resumes the client from the table's head.

    A synthetic hub id only reads as new to such a stream when it is above
    this, so a case that publishes one derives it from here rather than
    spelling a literal — a literal sits below the head on any database that
    already holds outbox rows (a second run, a suite that ran first), and the
    row then arrives as a straggler instead of the thing the case is about.

    Read it BEFORE the stream opens, never after. Nothing commits in between,
    so the number is the same either way — but this is a database round trip,
    and a round trip taken while a clock-bounded stream is already running
    spends that stream's whole budget on however long the one shared Postgres
    takes to answer. Every publish after it then lands on a stream the
    deadline already closed, and the case reports an empty body for a
    database that was busy.
    """
    async with AsyncSessionLocal() as session:
        return await latest_id(session)


async def _settled(app: FastAPI) -> None:
    """Wait until the outbox listener has carried every row committed so far
    into the hub. Call it before opening a stream whose frames are an exact
    list — otherwise the list is the case's rows PLUS however far behind the
    listener happened to be.

    A cursor says "I hold everything up to this id", and the replay honours
    it. An open stream does not: a row at or below the cursor that the hub
    carries while the stream is running is framed anyway, on purpose, because
    the route cannot tell a row that committed late from one the listener read
    late and a connected portal that is never told is stale (the straggler
    cases below pin that). Until the listener has read it, every row a case
    commits while setting up is such a row — the drive and team folders a team
    creation writes, the membership an ``add_member`` announces, the cursor
    itself — so a stream opened while the listener is still behind is handed
    the case's own setup as live frames, below the cursor it just resumed from.
    That is what put seven ``file_node.changed`` rows on a stream that asked
    for what came after them.

    The listener publishes a row and then moves its cursor past it, so a cursor
    at or above the head means everything up to that head is already in the
    subscribers' queues — and this stream has not subscribed yet.
    """
    runtime = runtime_of(app)
    assert runtime is not None and runtime.listener is not None
    listener = runtime.listener
    head = await _head()
    await _until(
        lambda: listener.last_seen_id >= head,
        what=f"the outbox listener carries every row up to {head}",
    )


def _durable(org_id: UUID, id: int, **overrides: Any) -> HubEvent:
    base: dict[str, Any] = {
        "lane": "durable",
        "org_id": org_id,
        "type": EventType.KB_ITEM_CHANGED.value,
        "entity": "kb_item",
        "entity_id": "item-1",
        "version": 1,
        "visibility": "org",
        "payload": {},
        "id": id,
    }
    base.update(overrides)
    return HubEvent(**base)


async def _until(
    predicate: Callable[[], bool], seconds: float = WAIT, *, what: str = "condition"
) -> None:
    """Wait for ``predicate``, and name the claim when it never comes —
    "condition not met in time" tells the next reader nothing about which of
    the fourteen waits in this file gave up."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"{what} not met within {seconds:.1f}s")
        await asyncio.sleep(0.01)


async def _logged_in(app: FastAPI, email: str, password: str) -> AsyncClient:
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": settings.frontend_base_url},
    )
    await login(client, email, password)
    return client


async def _member_client(
    app: FastAPI, real_session: AsyncSession, org_id: UUID, *, team_id: UUID | None = None
) -> tuple[User, AsyncClient]:
    user, password = await make_member(real_session, org_id=org_id, verified=True)
    if team_id is not None:
        await membership_service.add_member(
            real_session, team_id=team_id, user_id=user.id, role=TeamRole.MEMBER
        )
        await real_session.commit()
    assert password is not None
    return user, await _logged_in(app, user.email, password)


def _runtime_consumers(app: Any) -> list[object]:
    """The subscriptions the runtime itself holds, besides any stream's: the
    transcript watch that wakes the Slack relay, and the notebook service's
    kernel-event feed and caret board."""
    runtime = runtime_of(app)
    if runtime is None:
        return []
    held: list[object] = [runtime.watcher] if runtime.watcher is not None else []
    if runtime.notebooks is not None:
        held += [runtime.notebooks.feed, runtime.notebooks.carets]
    return held


def _hub(app: FastAPI) -> EventHub:
    runtime = runtime_of(app)
    assert runtime is not None
    return runtime.hub


async def _stream_in_background(
    app: FastAPI, client: AsyncClient, **kwargs: Any
) -> asyncio.Task[Response]:
    """Start a stream request and return once the server has subscribed, so a
    publish after this point is a live event, not history.

    Subscribing is an EDGE, and the hub only exposes the level: a stream that
    subscribed and then ended on its own deadline leaves the count back where
    it started, and a poll that lands either side of that reads as "it never
    subscribed" — which is how a loaded runner turned nine cases here into a
    five-second wait for something that had already happened. The high-water
    mark latches the edge; and a request that has finished can never subscribe
    again, so waiting on it is over either way — the case then fails on what it
    actually asserts about the body, which says far more than a timeout.

    A request that finished without ever subscribing is the other thing that
    latch admits, and it is not the same: it was REFUSED (or it raised), there
    is no stream behind it, and a case that read its body would report an empty
    frame list and name nothing. That one fails here, carrying the status.
    """
    hub = _hub(app)
    before = hub.subscriber_count
    high_water = before
    task = asyncio.create_task(client.get(STREAM, **kwargs))

    def opened() -> bool:
        nonlocal high_water
        high_water = max(high_water, hub.subscriber_count)
        return high_water > before or task.done()

    await _until(opened, what="the stream subscribes to the hub")
    if high_water == before and task.done():
        # ``result()`` re-raises, so a request that blew up says so instead of
        # being read as a stream that closed.
        resp = task.result()
        assert resp.status_code == 200, (
            f"the stream was answered {resp.status_code}, not opened: {resp.text[:200]}"
        )
    return task


@asynccontextmanager
async def _held_stream(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> AsyncIterator[asyncio.Task[Response]]:
    """A stream the CASE ends, for the cases that need one open.

    Every other case here lets the stream end on the file's bound and reads the
    body the server closed. A cap case cannot: what it asserts is a refusal
    *behind an open stream*, so that bound would be racing the case — the
    refusal has to be served before the stream runs out, and on a loaded runner
    it is not. The slot is then free, the refusal never comes, and the case
    fails with a 200 that says nothing about the cap.

    So the stream gets a bound no run reaches, and the block ends it by
    dropping the client — the disconnect that frees a slot in production — and
    does not return until the slot is actually back, so what the case asserts
    after the block is about capacity and not about timing. The keepalive is
    held off the same way: a tick re-checks the session against the database,
    and a database that refuses the check ends the stream with a ``retry`` and
    frees the slot — on the one Postgres a loaded run shares, that is the last
    way the runner could end a stream the case is holding. A cap says nothing
    about the tick, so the held stream never takes one. The file's own bounds
    are put back on the way out, for the stream a case opens next.
    """
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", HELD_STREAM_SECONDS)
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", HELD_STREAM_SECONDS)
    gate = ConnectionGate.sse()
    before = gate.active
    task = await _stream_in_background(app, client, **kwargs)
    assert not task.done(), "the held stream ended before the case could hold anything behind it"
    assert gate.active == before + 1, "an open stream holds a slot"
    try:
        yield task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await _until(lambda: gate.active == before, what="the held stream gives its slot back")
        monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", DEADLINE)
        monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", KEEPALIVE)


async def _refused(client: AsyncClient, **kwargs: Any) -> Response:
    """The answer to a request made behind a held stream.

    A refusal is immediate. A cap that stopped refusing would instead open a
    stream — and a held stream's bound is an hour — so a broken cap would hang
    the run rather than fail it. Bounded here, it fails saying what it found.
    """
    try:
        return await asyncio.wait_for(client.get(STREAM, **kwargs), WAIT)
    except TimeoutError:
        raise AssertionError(
            f"the capped request was not refused within {WAIT:.1f}s — it opened a stream"
        ) from None


# ---------------------------------------------------------------------------
# Authentication and gating
# ---------------------------------------------------------------------------


async def test_requires_a_session(client: AsyncClient, realtime_app: FastAPI) -> None:
    resp = await client.get(STREAM)
    assert resp.status_code == 401


async def test_a_query_string_token_is_never_a_credential(
    client: AsyncClient, realtime_app: FastAPI, org_admin: OrgWithAdmin
) -> None:
    """A JWT in a URL lands in every access log on the path; the stream only
    ever reads the cookie or the Authorization header."""
    token, claims = encode_session_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
        platform_role=None,
    )
    async with AsyncSessionLocal() as db:
        await register_token(db, claims=claims, token_type=TokenType.SESSION)
        await db.commit()
    assert (await client.get(STREAM, params={"token": token})).status_code == 401
    assert (await client.get(STREAM, params={"ticket": token})).status_code == 401
    # The same token in the header is accepted (and the stream then runs).
    ok = await client.get(STREAM, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


async def test_a_blocked_unverified_account_is_gated(
    realtime_app: FastAPI, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    member.created_at = datetime.now(UTC) - timedelta(days=365)
    await real_session.commit()
    assert password is not None
    c = await _logged_in(realtime_app, member.email, password)
    async with aclosing(c):
        resp = await c.get(STREAM)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_verification_required"


async def test_without_a_running_runtime_the_route_refuses_with_503(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """No lifespan ran (``ASGITransport`` never does): a stream here could never
    receive anything, and the honest answer is 'not running', not a silent
    stream of keepalives."""
    assert runtime_of(fastapi_app) is None
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(STREAM)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    # A 5xx body is deliberately generic (the envelope never leaks a server
    # detail to a caller); the status and the retry hint are the contract.
    assert not resp.headers["content-type"].startswith("text/event-stream")
    assert ConnectionGate.sse().active == 0, "a refused stream holds no slot"


async def test_a_process_with_the_listener_disabled_refuses_the_stream_with_503(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the outbox listener feeds the hub a stream drains, so a process
    built without one would serve keepalives forever and never an event — and
    the portal, which stops polling while the stream is up, would quietly stop
    refreshing altogether. The honest answer is 503 with a retry hint."""
    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    runtime = await realtime_runtime.start(fastapi_app, decide=decide_on_record)
    try:
        assert runtime.listener is None, "the setting is what decides"
        await login(client, org_admin.admin_email, org_admin.admin_password)
        resp = await client.get(STREAM)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"
        assert not resp.headers["content-type"].startswith("text/event-stream")
        assert ConnectionGate.sse().active == 0, "a refused stream holds no slot"
    finally:
        await realtime_runtime.stop(fastapi_app, runtime)


async def test_a_listener_that_never_connects_is_waited_for_and_then_refused(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener that exists but has no connection delivers nothing either.
    The request waits a bounded moment for it — a process booting or a listener
    mid-reconnect must not be refused over a blip — and then answers 503."""
    monkeypatch.setattr(realtime_runtime, "LISTENER_READY_SECONDS", 0.05)
    runtime = runtime_of(realtime_app)
    assert runtime is not None and runtime.listener is not None
    monkeypatch.setattr(runtime.listener, "_connected", False)
    monkeypatch.setattr(runtime.listener, "_connected_event", asyncio.Event())
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(STREAM)
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    assert ConnectionGate.sse().active == 0


async def test_a_listener_that_connects_during_the_wait_gets_its_stream(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the bounded wait: a listener that comes back inside it
    is served, so a reconnect is not turned into a refusal."""
    runtime = runtime_of(realtime_app)
    assert runtime is not None and runtime.listener is not None
    listener = runtime.listener
    reconnecting = asyncio.Event()
    monkeypatch.setattr(listener, "_connected", False)
    monkeypatch.setattr(listener, "_connected_event", reconnecting)

    async def _reconnect() -> None:
        await asyncio.sleep(0.05)
        monkeypatch.setattr(listener, "_connected", True)
        reconnecting.set()

    task = asyncio.create_task(_reconnect())
    try:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        resp = await client.get(STREAM)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
    finally:
        await task


# ---------------------------------------------------------------------------
# The stream itself
# ---------------------------------------------------------------------------


async def test_stream_opens_with_retry_and_connected_and_closes_at_the_deadline(
    client: AsyncClient, realtime_app: FastAPI, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(STREAM)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.text.startswith("retry: 2000\n\n: connected\n\n")
    assert resp.text.endswith("retry: 1000\n\n"), "the deadline closes with a reconnect hint"
    assert "keepalive" in _comments(resp.text)
    assert _events(resp.text) == []


async def test_the_slot_and_the_subscription_are_released_when_the_stream_ends(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Half of what this reads — the held slot — is read while the stream is
    # still open, so the bound is the one for a case that acts on an open
    # stream rather than the replay default, which a starved loop could close
    # between the subscribe edge and the read.
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # The runtime's own consumers (the transcript watch) stay subscribed.
    base = _hub(realtime_app).subscriber_count
    task = await _stream_in_background(realtime_app, client)
    assert ConnectionGate.sse().active == 1
    assert ConnectionGate.sse().active_for(str(org_admin.admin_id)) == 1
    await task
    assert ConnectionGate.sse().active == 0
    assert _hub(realtime_app).subscriber_count == base


async def test_live_hub_events_are_framed_thin_and_only_durable_client_types_pass(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        hub = _hub(realtime_app)
        head = await _head()
        hub.publish(_durable(org_admin.org_id, head + 1, payload={"secret": "never on the wire"}))
        hub.publish(_durable(org_admin.org_id, None, lane="ephemeral", type="presence"))
        hub.publish(_durable(org_admin.org_id, head + 2, type=EventType.DOC_OP.value))
        hub.publish(_durable(org_admin.org_id, head + 3, type=EventType.AUTHZ_DECISION.value))
        hub.publish(_durable(uuid4(), head + 4))
        await stream.wait_for_event("kb_item.changed")
        # The four that must NOT be framed were published BEFORE the one that
        # was, so a quiet moment after it is when their absence is a claim.
        await stream.quiet()
    got = stream.events
    assert [f["id"] for f in got] == [str(head + 1)]
    assert got[0]["event"] == "kb_item.changed"
    data = json.loads(got[0]["data"])
    assert data == {
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "item-1",
        "version": 1,
        "org_id": str(org_admin.org_id),
    }
    assert "secret" not in stream.text


async def test_a_row_committed_by_another_session_arrives_through_the_listener(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path on one loop: outbox row → NOTIFY → the runtime's
    listener → the hub → the frame."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", COMMIT_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        row_id = await _emit(org_admin.org_id, entity_id="live-row", version=4)
        await stream.wait_for_event("kb_item.changed")
        await stream.quiet()
    got = stream.events
    assert [f["id"] for f in got] == [str(row_id)]
    assert json.loads(got[0]["data"])["entity_id"] == "live-row"
    assert json.loads(got[0]["data"])["version"] == 4


async def test_the_hub_event_id_below_the_cursor_is_not_re_sent(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row the catch-up already framed can arrive again through the hub (it
    committed between the subscribe and the read), and so can the row the
    client named as its cursor; the stream de-duplicates by id so a client
    never sees a frame twice."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first = await _emit(org_admin.org_id, entity_id="a")
    second = await _emit(org_admin.org_id, entity_id="b")
    async with open_event_stream(
        realtime_app, client, headers={"Last-Event-ID": str(first)}
    ) as stream:
        # ``second`` is replayed by the catch-up, so the stream has already
        # framed it by the time it says it is connected.
        await stream.opened()
        _hub(realtime_app).publish(_durable(org_admin.org_id, second, entity_id="b"))
        _hub(realtime_app).publish(_durable(org_admin.org_id, first, entity_id="a"))
        await stream.wait_for_event("kb_item.changed")
        await stream.quiet()
    assert [f["id"] for f in stream.events] == [str(second)]
    assert "\nid: " not in stream.text.split(sse_frame_of(second))[1], (
        "a duplicate is dropped silently — it leaves no frame and no cursor line"
    )


def sse_frame_of(row_id: int) -> str:
    return f"id: {row_id}\nevent:"


def _cursor_only_frames(body: str) -> list[str]:
    """The ``id:``-only frames (no event, no data) in ``body``, in order."""
    return [f["id"] for f in _frames(body) if set(f) == {"id"}]


# ---------------------------------------------------------------------------
# Stragglers: a row that commits after a higher id was already framed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_cursor", [False, True], ids=["fresh-connect", "resumed"])
async def test_a_hub_event_with_a_lower_id_than_one_already_framed_is_still_framed(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    with_cursor: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outbox ids are not framed in order. A straggler is framed with its own
    id; right after it the stream writes an id-only frame carrying the cursor
    the client held, so the resume cursor never moves backwards. A duplicate
    delivery of either row leaves no second frame."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    headers: dict[str, str] = {}
    if with_cursor:
        headers["Last-Event-ID"] = str(await _emit(org_admin.org_id, entity_id="cursor"))
    async with open_event_stream(realtime_app, client, headers=headers) as stream:
        await stream.opened()
        hub = _hub(realtime_app)
        # Both ids sit above the head this stream opened at, so the overtaker
        # really does advance its cursor and the straggler really does arrive
        # behind that cursor — the ordering this case is about, wherever the
        # outbox sequence happens to stand.
        head = await _head()
        overtaker, straggler = head + 5, head + 2
        hub.publish(_durable(org_admin.org_id, overtaker, entity_id="overtaker"))
        hub.publish(_durable(org_admin.org_id, straggler, entity_id="straggler"))
        hub.publish(_durable(org_admin.org_id, straggler, entity_id="straggler"))
        hub.publish(_durable(org_admin.org_id, overtaker, entity_id="overtaker"))
        # Both frames and the cursor line behind them, then a quiet moment —
        # the duplicates were published before it, so their absence is a claim.
        await stream.wait_for(
            lambda s: len(s.events) == 2, what="the overtaker's frame and the straggler's"
        )
        await stream.quiet()
    got = stream.events
    assert [(f["id"], json.loads(f["data"])["entity_id"]) for f in got] == [
        (str(overtaker), "overtaker"),
        (str(straggler), "straggler"),
    ]
    assert _cursor_only_frames(stream.text) == [str(overtaker)], (
        "exactly one cursor line, after the straggler"
    )
    after_straggler = stream.text.split(sse_frame_of(straggler), 1)[1]
    assert after_straggler.split("\n\n", 1)[1].startswith(f"id: {overtaker}\n\n"), (
        "the cursor line comes right after the straggler's own frame"
    )


async def test_a_row_that_commits_after_a_higher_one_reaches_an_open_stream(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
) -> None:
    """The whole path with real transactions: one row takes its id first but
    commits second, so the listener reads the higher id, then learns of the
    lower one by its notified id. A connected client must be told about both —
    a straggler dropped here is an invalidation the portal never gets."""
    runtime = runtime_of(realtime_app)
    assert runtime is not None and runtime.listener is not None
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        async with AsyncSessionLocal() as slow:
            straggler = int(
                (
                    await emit(
                        slow,
                        org_id=org_admin.org_id,
                        type=EventType.KB_ITEM_CHANGED,
                        entity="kb_item",
                        entity_id="straggler",
                        version=1,
                    )
                ).id
            )
            overtaker = await _emit(org_admin.org_id, entity_id="overtaker")
            assert straggler < overtaker
            listener = runtime.listener
            await _until(
                lambda: listener.last_seen_id >= overtaker,
                what="the outbox listener reaches the overtaker",
            )
            await slow.commit()
        await stream.wait_for(
            lambda s: len(s.events) == 2, what="the overtaker's frame and the straggler's"
        )
        await stream.quiet()
    got = stream.events
    assert [(f["id"], json.loads(f["data"])["entity_id"]) for f in got] == [
        (str(overtaker), "overtaker"),
        (str(straggler), "straggler"),
    ]
    assert _cursor_only_frames(stream.text) == [str(overtaker)]


async def test_after_a_straggler_a_reconnect_replays_only_what_came_after_the_cursor(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
) -> None:
    """The cursor line after a straggler is what a client resumes from: with
    it the gap is exactly the rows after the highest id it covered. A client
    that resumed from the straggler's own id instead would be re-sent the row
    that overtook it — the documented cost of a cursor that moved backwards."""
    runtime = runtime_of(realtime_app)
    assert runtime is not None and runtime.listener is not None
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with open_event_stream(realtime_app, client) as first:
        await first.opened()
        async with AsyncSessionLocal() as slow:
            straggler = int(
                (
                    await emit(
                        slow,
                        org_id=org_admin.org_id,
                        type=EventType.KB_ITEM_CHANGED,
                        entity="kb_item",
                        entity_id="straggler",
                        version=1,
                    )
                ).id
            )
            overtaker = await _emit(org_admin.org_id, entity_id="overtaker")
            listener = runtime.listener
            await _until(
                lambda: listener.last_seen_id >= overtaker,
                what="the outbox listener reaches the overtaker",
            )
            await slow.commit()
        await first.wait_for(
            lambda s: len(s.events) == 2, what="the overtaker's frame and the straggler's"
        )
    assert [f["id"] for f in first.events] == [str(overtaker), str(straggler)]
    later = await _emit(org_admin.org_id, entity_id="later")

    resumed = await client.get(STREAM, headers={"Last-Event-ID": str(overtaker)})
    assert [f["id"] for f in _events(resumed.text)] == [str(later)]
    assert _named(resumed.text, "reset") == []

    regressed = await client.get(STREAM, params={"after": str(straggler)})
    assert [f["id"] for f in _events(regressed.text)] == [str(overtaker), str(later)]


async def test_a_straggler_the_hub_carried_while_the_client_was_away_is_not_replayed(
    client: AsyncClient, realtime_app: FastAPI, org_admin: OrgWithAdmin
) -> None:
    """A row that commits with a lower id than the client's cursor is below
    that cursor, and the catch-up read looks above it: a client that was away
    when the hub carried it is never told. Pinned so the gap is a known one,
    not an accident — closing it needs the listener's straggler window on disk.

    The limit is the REPLAY's, and only the replay's: the same row reaching an
    open stream IS framed (the case below), which is why this one does not
    reconnect until the hub has actually carried the straggler with nobody
    subscribed — the disconnected window the claim is about. Left to chance,
    the listener forwards it either side of the reconnect depending on how
    busy the runner is, and the case reads as the machine's load rather than
    as the cursor.
    """
    runtime = runtime_of(realtime_app)
    assert runtime is not None and runtime.listener is not None
    listener = runtime.listener
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as slow:
        straggler = int(
            (
                await emit(
                    slow,
                    org_id=org_admin.org_id,
                    type=EventType.KB_ITEM_CHANGED,
                    entity="kb_item",
                    entity_id="straggler",
                    version=1,
                )
            ).id
        )
        # What the client would have been sent had it been here. Subscribed to
        # that one id, so the queue behind it cannot fill: this listener reads
        # every row of an outbox the whole run commits to.
        away = _hub(realtime_app).subscribe(lambda e: e.id == straggler, label="away")
        try:
            overtaker = await _emit(org_admin.org_id, entity_id="overtaker")
            assert straggler < overtaker
            await _until(
                lambda: listener.last_seen_id >= overtaker,
                what="the outbox listener reaches the overtaker",
            )
            await slow.commit()
            await _until(
                lambda: not away.queue.empty(),
                what="the hub carries the straggler while the client is away",
            )
        finally:
            _hub(realtime_app).unsubscribe(away)
    later = await _emit(org_admin.org_id, entity_id="later")

    resp = await client.get(STREAM, headers={"Last-Event-ID": str(overtaker)})

    assert [f["id"] for f in _events(resp.text)] == [str(later)]


async def test_a_straggler_that_reaches_a_resumed_stream_is_framed_below_its_cursor(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of that limit, and the reason the replay's gap is a gap
    rather than a rule: a row below the id the client RESUMED from still
    reaches it when the hub carries it while the stream is open. Dropping it
    would leave a connected portal stale about a row nothing else will ever
    replay. The cursor line right after it is what keeps the resume honest —
    it goes back above the straggler, never to the straggler's own id.

    The hub carries the straggler here rather than a commit travelling
    ``NOTIFY``: which side of the reconnect the listener lands on is the
    timing this pair stopped depending on, and the route reads the same event
    either way.
    """
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    straggler = await _emit(org_admin.org_id, entity_id="straggler")
    overtaker = await _emit(org_admin.org_id, entity_id="overtaker")
    task = await _stream_in_background(
        realtime_app, client, headers={"Last-Event-ID": str(overtaker)}
    )
    _hub(realtime_app).publish(_durable(org_admin.org_id, straggler, entity_id="straggler"))
    resp = await task
    assert [(f["id"], json.loads(f["data"])["entity_id"]) for f in _events(resp.text)] == [
        (str(straggler), "straggler")
    ]
    cursors = _cursor_only_frames(resp.text)
    # Not the overtaker's id itself: the cursor a stream resumes clients from
    # is the head of the whole outbox, which every other org in the run is
    # also committing to.
    assert len(cursors) == 1 and int(cursors[0]) >= overtaker, (
        "one cursor line, and it puts the resume cursor back above the straggler"
    )


# ---------------------------------------------------------------------------
# Cursors and replay
# ---------------------------------------------------------------------------


async def test_last_event_id_replays_only_this_orgs_rows_after_the_cursor(
    client: AsyncClient, realtime_app: FastAPI, org_admin: OrgWithAdmin
) -> None:
    other_org = uuid4()
    before = await _emit(org_admin.org_id, entity_id="before")
    mine_1 = await _emit(org_admin.org_id, entity_id="mine-1", version=2)
    await _emit(other_org, entity_id="theirs")
    mine_2 = await _emit(org_admin.org_id, entity_id="mine-2", version=3)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    resp = await client.get(STREAM, headers={"Last-Event-ID": str(before)})

    got = _events(resp.text)
    assert [f["id"] for f in got] == [str(mine_1), str(mine_2)]
    assert [json.loads(f["data"])["entity_id"] for f in got] == ["mine-1", "mine-2"]
    assert "theirs" not in resp.text
    assert "before" not in resp.text
    assert _named(resp.text, "reset") == []
    # The replay comes right after the opening frames, before any keepalive.
    assert resp.text.index("id: ") < resp.text.index(": keepalive")


@pytest.mark.parametrize(
    ("use_header", "use_query", "expect_from"),
    [
        pytest.param(False, True, "query", id="query-alone-resumes"),
        pytest.param(True, False, "header", id="header-alone-resumes"),
        pytest.param(True, True, "header", id="header-wins-over-query"),
    ],
)
async def test_the_after_query_resumes_and_the_header_wins(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    use_header: bool,
    use_query: bool,
    expect_from: str,
) -> None:
    """A browser cannot set ``Last-Event-ID`` on a reconnect the client owns,
    so ``?after=`` carries the cursor then; when both are present the header
    is the newer of the two."""
    older = await _emit(org_admin.org_id, entity_id="older")
    newer = await _emit(org_admin.org_id, entity_id="newer")
    last = await _emit(org_admin.org_id, entity_id="last")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    headers = {"Last-Event-ID": str(newer)} if use_header else {}
    params = {"after": str(older)} if use_query else {}
    # The oldest row is below the header's cursor: whichever cursor wins, the
    # rows this case wrote are what the case compares against, not the ones the
    # listener had not carried yet.
    await _settled(realtime_app)

    resp = await client.get(STREAM, headers=headers, params=params)

    ids = [f["id"] for f in _events(resp.text)]
    if expect_from == "query":
        assert ids == [str(newer), str(last)]
    else:
        assert ids == [str(last)]


async def test_a_cursor_ahead_of_the_head_gets_a_reset(
    client: AsyncClient, realtime_app: FastAPI, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(STREAM, headers={"Last-Event-ID": str(2**62)})
    resets = _named(resp.text, "reset")
    assert len(resets) == 1
    assert json.loads(resets[0]["data"]) == {"reason": "cursor_ahead"}
    assert "id" not in resets[0], "a reset never moves the cursor"
    assert _events(resp.text) == []


@pytest.mark.parametrize(
    ("headers", "params"),
    [
        pytest.param({"Last-Event-ID": "abc"}, {}, id="header-text"),
        pytest.param({"Last-Event-ID": "-1"}, {}, id="header-negative"),
        pytest.param({"Last-Event-ID": ""}, {}, id="header-empty"),
        pytest.param({}, {"after": "abc"}, id="query-text"),
        pytest.param({}, {"after": "-5"}, id="query-negative"),
        pytest.param({}, {"after": "1.5"}, id="query-fraction"),
    ],
)
async def test_an_unusable_cursor_is_ignored_not_an_error(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    headers: dict[str, str],
    params: dict[str, str],
) -> None:
    await _emit(org_admin.org_id, entity_id="history")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # No cursor means no replay — and nothing left in the listener to arrive
    # live either, which is the other way this body could hold a frame.
    await _settled(realtime_app)
    resp = await client.get(STREAM, headers=headers, params=params)
    assert resp.status_code == 200
    assert _events(resp.text) == [], "no cursor means no replay"
    assert _named(resp.text, "reset") == []


async def test_a_cursor_too_far_behind_gets_a_reset_instead_of_an_unbounded_replay(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_route, "CATCH_UP_PAGE", 2)
    monkeypatch.setattr(events_route, "MAX_CATCH_UP_ROWS", 3)
    cursor = await _emit(org_admin.org_id, entity_id="cursor")
    for i in range(5):
        await _emit(org_admin.org_id, entity_id=f"row-{i}")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # A reset replays nothing, so the stream holds none of these ids against a
    # live delivery of the same rows: the case asserts an empty replay, and
    # every row it wrote has to be behind it before the stream opens.
    await _settled(realtime_app)

    resp = await client.get(STREAM, headers={"Last-Event-ID": str(cursor)})

    resets = _named(resp.text, "reset")
    assert [json.loads(r["data"]) for r in resets] == [{"reason": "cursor_too_old"}]
    assert _events(resp.text) == []


async def test_a_replay_within_the_cap_pages_through_every_row(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_route, "CATCH_UP_PAGE", 2)
    cursor = await _emit(org_admin.org_id, entity_id="cursor")
    expected = [str(await _emit(org_admin.org_id, entity_id=f"row-{i}")) for i in range(5)]
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
    assert [f["id"] for f in _events(resp.text)] == expected


# ---------------------------------------------------------------------------
# Visibility and entitlement filtering
# ---------------------------------------------------------------------------


async def test_user_visibility_rows_reach_only_that_user(
    realtime_app: FastAPI, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    alice, alice_client = await _member_client(realtime_app, real_session, org_admin.org_id)
    bob, bob_client = await _member_client(realtime_app, real_session, org_admin.org_id)
    cursor = await _emit(org_admin.org_id, entity_id="cursor")
    for_alice = await _emit(org_admin.org_id, visibility=f"user:{alice.id}", entity_id="alice")
    for_bob = await _emit(org_admin.org_id, visibility=f"user:{bob.id}", entity_id="bob")
    for_all = await _emit(org_admin.org_id, entity_id="everyone")
    async with aclosing(alice_client), aclosing(bob_client):
        a = await alice_client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
        b = await bob_client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
    assert [f["id"] for f in _events(a.text)] == [str(for_alice), str(for_all)]
    assert [f["id"] for f in _events(b.text)] == [str(for_bob), str(for_all)]


@pytest.mark.parametrize("viewer", ["member", "org-admin", "platform-staff"])
async def test_an_authorization_decision_row_never_reaches_a_tenant_stream(
    realtime_app: FastAPI,
    platform_support: OrgWithAdmin,
    real_session: AsyncSession,
    viewer: str,
) -> None:
    """Decision rows are written with ``visibility="platform"`` and the type
    ``authz.decision``; the type alone keeps them off the stream, so not even a
    platform-staff viewer (who may read platform-visibility rows) gets one."""
    org_id = platform_support.org_id
    member, member_client = await _member_client(realtime_app, real_session, org_id)
    admin_client = await _logged_in(
        realtime_app, platform_support.admin_email, platform_support.admin_password
    )
    cursor = await _emit(org_id, entity_id="cursor")
    async with AsyncSessionLocal() as session:
        decision = await emit(
            session,
            org_id=org_id,
            type=EventType.AUTHZ_DECISION,
            entity="connector",
            entity_id=uuid4().hex,
            visibility="platform",
            payload={"outcome": "deny", "reason": "not_entitled"},
        )
        await session.commit()
    visible = await _emit(org_id, entity_id="after-the-decision")
    if viewer == "member":
        stream_client = member_client
    else:
        stream_client = admin_client
    if viewer == "org-admin":
        # The same account with its platform role removed: an ordinary org admin.
        async with AsyncSessionLocal() as session:
            staff = await session.get(User, platform_support.admin_id)
            assert staff is not None
            staff.platform_role = None
            await session.commit()
    async with aclosing(member_client), aclosing(admin_client):
        resp = await stream_client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
    assert resp.status_code == 200
    ids = [f["id"] for f in _events(resp.text)]
    assert str(decision.id) not in ids
    assert ids == [str(visible)]
    assert all(f["event"] != EventType.AUTHZ_DECISION.value for f in _events(resp.text))
    assert member.home_org_team_id == org_id


async def test_platform_visibility_requires_a_platform_role(
    realtime_app: FastAPI, platform_support: OrgWithAdmin, real_session: AsyncSession
) -> None:
    _member, member_client = await _member_client(
        realtime_app, real_session, platform_support.org_id
    )
    cursor = await _emit(platform_support.org_id, entity_id="cursor")
    staff_row = await _emit(platform_support.org_id, visibility="platform", entity_id="staff")
    staff_client = await _logged_in(
        realtime_app, platform_support.admin_email, platform_support.admin_password
    )
    async with aclosing(staff_client), aclosing(member_client):
        staff = await staff_client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
        member = await member_client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
    assert [f["id"] for f in _events(staff.text)] == [str(staff_row)]
    assert _events(member.text) == []


@pytest.mark.parametrize(
    ("who", "sees"),
    [
        pytest.param("team-member", True, id="member-of-the-team"),
        pytest.param("sibling-member", False, id="member-of-a-sibling-team"),
        pytest.param("root-member", False, id="plain-org-member"),
        pytest.param("org-admin", True, id="org-admin"),
    ],
)
async def test_team_scoped_rows_need_a_membership_or_org_admin(
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    who: str,
    sees: bool,
) -> None:
    eng = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Eng", parent_team_id=org_admin.org_id
    )
    data = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=eng.id
    )
    platform = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Platform", parent_team_id=eng.id
    )
    await real_session.commit()
    if who == "org-admin":
        client = await _logged_in(realtime_app, org_admin.admin_email, org_admin.admin_password)
    else:
        team = {"team-member": data.id, "sibling-member": platform.id, "root-member": None}[who]
        _user, client = await _member_client(
            realtime_app, real_session, org_admin.org_id, team_id=team
        )
    cursor = await _emit(org_admin.org_id, entity_id="cursor")
    scoped = await _emit(
        org_admin.org_id,
        type=EventType.TEAM_CONNECTION_UPDATED,
        entity_id="conn",
        payload={"team_id": str(data.id)},
    )
    # Creating the three teams wrote the org's drive and a folder per team, and
    # a member who joins one gets a home folder as well: org-visible rows with
    # no team of their own, which every member of this org may see. They are
    # history the cursor covers, so they have to be carried before the stream
    # opens rather than into it.
    await _settled(realtime_app)
    async with aclosing(client):
        resp = await client.get(STREAM, headers={"Last-Event-ID": str(cursor)})
    ids = [f["id"] for f in _events(resp.text)]
    assert ids == ([str(scoped)] if sees else [])


async def test_a_membership_granted_mid_stream_takes_effect_within_one_keepalive(
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keepalive tick refreshes the entitlement snapshot, so a user added
    to a team starts receiving that team's rows without reconnecting.

    The second row is published only once a tick has provably put the new team
    into the snapshot the stream filters on, rather than after a sleep of a few
    keepalive intervals: on a loaded machine the tick can be later than any
    sleep a test would pick, and the row would then be dropped for the reason
    the test is meant to prove is gone.
    """
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", REFRESH_STREAM_SECONDS)
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    refreshed: set[UUID] = set()
    recheck = events_route._recheck

    async def _recording_recheck(claims: Any, user_id: UUID, ref: EntitlementRef) -> bool:
        """The real re-check, with the snapshot it installed made observable."""
        alive = await recheck(claims, user_id, ref)
        refreshed.update(ref.value.team_ids)
        return alive

    monkeypatch.setattr(events_route, "_recheck", _recording_recheck)
    member, client = await _member_client(realtime_app, real_session, org_admin.org_id)
    async with aclosing(client):
        # Above the head the stream opened at, and with room between them for
        # the rows the grant itself commits, so neither published id can
        # collide with one the outbox hands out during this stream.
        head = await _head()
        task = await _stream_in_background(realtime_app, client)
        hub = _hub(realtime_app)
        before_grant, after_grant = head + 10, head + 20
        hub.publish(
            _durable(
                org_admin.org_id,
                before_grant,
                entity_id="before-grant",
                payload={"team_id": str(team.id)},
            )
        )
        await membership_service.add_member(
            real_session, team_id=team.id, user_id=member.id, role=TeamRole.MEMBER
        )
        await real_session.commit()
        await _until(
            lambda: team.id in refreshed,
            REFRESH_WAIT_SECONDS,
            what="the keepalive re-checks the entitlement",
        )
        # The stream outlives the wait by construction, so a row published here
        # cannot be missed because the deadline already closed it.
        assert _hub(realtime_app).subscriber_count == 1 + len(_runtime_consumers(realtime_app))
        hub.publish(
            _durable(
                org_admin.org_id,
                after_grant,
                entity_id="after-grant",
                payload={"team_id": str(team.id)},
            )
        )
        resp = await task
    # The grant commits a membership row of its own, which the listener may
    # deliver to this stream once the refreshed snapshot admits it. Whether
    # that lands inside this stream's deadline is a race and not the point, so
    # the case reads only the two rows it published itself: the one from before
    # the grant must be absent and the one from after it present.
    mine = [
        (f["id"], json.loads(f["data"])["entity_id"])
        for f in _events(resp.text)
        if json.loads(f["data"])["entity_id"] in {"before-grant", "after-grant"}
    ]
    assert mine == [(str(after_grant), "after-grant")]


# ---------------------------------------------------------------------------
# Keepalive re-auth
# ---------------------------------------------------------------------------


async def test_revocation_mid_stream_ends_it_with_an_error_frame(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", WRITE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    task = await _stream_in_background(realtime_app, client)
    async with AsyncSessionLocal() as db:
        await revoke_all_for_user(db, org_admin.admin_id)
        await db.commit()
    resp = await task
    errors = _named(resp.text, "error")
    assert [json.loads(e["data"]) for e in errors] == [{"code": "unauthorized"}]
    assert resp.text.endswith(errors[0]["data"] + "\n\n"), "the error frame is the last thing sent"
    assert not resp.text.endswith("retry: 1000\n\n")
    # The reconnect is refused outright.
    assert (await client.get(STREAM)).status_code == 401


async def test_a_session_that_expires_mid_stream_is_closed_at_the_next_keepalive(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential that was valid at connect time does not stay valid for the
    life of the stream. Nothing re-reads the token once the generator is
    running — the entry check is the only decode — so the keepalive tick owns
    the expiry, and without it an expired session keeps receiving events until
    the stream deadline (fifty minutes in production).

    The expiry is the case's to spend, not the runner's. A token short enough
    to age out on its own has to be minted, registered and connected with
    before it dies, and on a loaded machine it dies first: the stream is
    answered 401, there is nothing to tick, and the case reports a missing
    frame that was never about the product. Under a clock the case holds, the
    token cannot expire while the connect is in flight and expires exactly when
    the case says. The loop keeps real time (``real_asyncio``), so the
    keepalive still ticks on its own interval.

    The stream's own deadline is then put OUT OF REACH rather than sized. Every
    other bound in the ladder is a backstop behind a claim the body carries;
    here the deadline is a rival ending — the one frame that means "the clock
    won" — so a stream bounded at a handful of seconds makes a loaded host,
    not the re-check, decide which ending the case reads. With the deadline
    unreachable the only ending that can appear is the one a tick produced, and
    the wait below is a stop rather than a budget: it says how long a re-check
    that never closes the stream is given before it is reported, and a host
    slow enough to spend it fails naming the tick that never came.
    """
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", HELD_STREAM_SECONDS)
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 2)
    with freeze_time(datetime.now(UTC), real_asyncio=True) as frozen:
        token, claims = encode_session_token(
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_team_id=org_admin.org_id,
            platform_role=None,
        )
        async with AsyncSessionLocal() as db:
            await register_token(db, claims=claims, token_type=TokenType.SESSION)
            await db.commit()
        auth = {"Authorization": f"Bearer {token}"}

        task = await _stream_in_background(realtime_app, client, headers=auth)
        # Valid at the entry check, expired from here: the mint truncates
        # ``iat`` to whole seconds, so three seconds is past any two-second
        # token. Only a tick can notice — nothing else re-reads the session.
        frozen.tick(timedelta(seconds=3))
        done, _ = await asyncio.wait([task], timeout=WAIT)
        if not done:
            task.cancel()
            pytest.fail(f"no keepalive tick closed the expired session within {WAIT:g}s")
        resp = task.result()

        errors = _named(resp.text, "error")
        assert [json.loads(e["data"]) for e in errors] == [{"code": "unauthorized"}]
        assert resp.text.endswith(errors[0]["data"] + "\n\n"), (
            "the error frame is the last thing sent"
        )
        assert not resp.text.endswith("retry: 1000\n\n"), (
            "it ended on the session, not the deadline"
        )
        assert _comments(resp.text).count("keepalive") >= 1, "a keepalive tick is what noticed"
        # And the reconnect the client would make is refused outright.
        assert (await client.get(STREAM, headers=auth)).status_code == 401


async def test_deactivation_mid_stream_ends_it_with_an_error_frame(
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", WRITE_STREAM_SECONDS)
    member, client = await _member_client(realtime_app, real_session, org_admin.org_id)
    async with aclosing(client):
        task = await _stream_in_background(realtime_app, client)
        member.is_active = False
        await real_session.commit()
        resp = await task
    assert [json.loads(e["data"]) for e in _named(resp.text, "error")] == [{"code": "unauthorized"}]


async def _last_used_at(user_id: UUID) -> datetime | None:
    from alkera_core.models import AuthToken
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(AuthToken.last_used_at)
                .where(AuthToken.user_id == user_id)
                .order_by(AuthToken.issued_at.desc())
                .limit(1)
            )
        ).scalar_one()


async def _backdate_last_used(user_id: UUID, *, seconds: int) -> datetime:
    from alkera_core.models import AuthToken
    from sqlalchemy import update

    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AuthToken).where(AuthToken.user_id == user_id).values(last_used_at=stamp)
        )
        await db.commit()
    return stamp


async def test_a_keepalive_recheck_is_not_activity(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held stream is not a person working: a tab left open on a desk ticks
    exactly like one being read, so the tick reads the idle window and never
    moves it. The window is aged past the thirty-second slide throttle while
    the stream runs, so a tick that DID count as activity would move the
    stamp off where the ageing put it."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", WRITE_STREAM_SECONDS)
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 3600)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    task = await _stream_in_background(realtime_app, client)
    aged = await _backdate_last_used(org_admin.admin_id, seconds=40)
    resp = await task
    assert _comments(resp.text).count("keepalive") >= 2
    assert _named(resp.text, "error") == [], "forty seconds idle is inside the window"
    assert await _last_used_at(org_admin.admin_id) == aged


async def test_a_keepalive_recheck_still_enforces_the_idle_window(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tick still enforces the window it does not move: a session already
    idle past it when the tick lands is ended by it, even though nothing
    revoked it."""
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", WRITE_STREAM_SECONDS)
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 5)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    task = await _stream_in_background(realtime_app, client)
    await _backdate_last_used(org_admin.admin_id, seconds=60)
    resp = await task
    assert [json.loads(e["data"]) for e in _named(resp.text, "error")] == [{"code": "unauthorized"}]


async def test_a_keepalive_recheck_does_not_slide_the_session_family(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
) -> None:
    """The window that actually keeps a browser signed in is the refresh
    family's. A stream that moved it on every tick would keep an unattended
    tab signed in until the absolute cap; only the person's own requests
    (a refresh rotation) move it."""
    from alkera_core.models import AuthRefreshToken
    from sqlalchemy import select, update

    await login(client, org_admin.admin_email, org_admin.admin_password)
    aged = datetime.now(UTC) + timedelta(seconds=60)

    async def _idle_expiry() -> datetime:
        async with AsyncSessionLocal() as db:
            return (
                await db.execute(
                    select(AuthRefreshToken.idle_expires_at)
                    .where(
                        AuthRefreshToken.user_id == org_admin.admin_id,
                        AuthRefreshToken.used_at.is_(None),
                        AuthRefreshToken.revoked_at.is_(None),
                    )
                    .order_by(AuthRefreshToken.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.user_id == org_admin.admin_id)
            .values(idle_expires_at=aged)
        )
        await db.commit()

    task = await _stream_in_background(realtime_app, client)
    resp = await task
    assert _named(resp.text, "error") == [], "a minute of window left is not an expired session"
    assert await _idle_expiry() == aged


# ---------------------------------------------------------------------------
# Overflow
# ---------------------------------------------------------------------------


async def test_a_subscriber_that_falls_behind_gets_a_reset_and_keeps_going(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", LIVE_STREAM_SECONDS)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    task = await _stream_in_background(realtime_app, client)
    hub = _hub(realtime_app)
    # More than the queue holds, published without yielding to the reader.
    from alkera_core.events.hub import DEFAULT_QUEUE_MAXSIZE

    for i in range(DEFAULT_QUEUE_MAXSIZE + 10):
        hub.publish(_durable(org_admin.org_id, 30_000 + i))
    await asyncio.sleep(0.05)
    hub.publish(_durable(org_admin.org_id, 40_000))
    resp = await task
    resets = _named(resp.text, "reset")
    assert [json.loads(r["data"]) for r in resets] == [{"reason": "overflow"}]
    ids = [f["id"] for f in _events(resp.text)]
    assert "40000" in ids, "delivery resumes after the reset is acknowledged"
    assert resp.text.index("event: reset") < resp.text.index("id: 40000")


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


async def test_the_per_user_cap_answers_429_with_retry_after_and_frees_up(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with _held_stream(realtime_app, client, monkeypatch) as task:
        refused = await _refused(client)
        assert not task.done(), "the refusal was answered while the stream it counts was open"
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "5"
        assert refused.json()["error"]["code"] == "too_many_streams"
        assert ConnectionGate.sse().active == 1, "a refused stream holds no slot"
    assert (await client.get(STREAM)).status_code == 200


@asynccontextmanager
async def parked_before_the_body(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> AsyncIterator[asyncio.Task[Response]]:
    """A stream request held in the gap between the route returning its
    response and the server starting to send it.

    That gap is real: the request's database session commits and closes there
    (``get_db`` is function-scoped), a Postgres round trip that a loaded
    server can take seconds over. A client that goes away inside it cancels
    the request before the response — and so before the body generator — ever
    runs. The commit of the stream's own session is parked until the request
    is cancelled, so the case lands the disconnect exactly there every run."""
    hub = _hub(app)
    subscribed_before = hub.subscriber_count
    parked = asyncio.Event()
    commit = AsyncSession.commit

    async def commit_parking_the_stream(self: AsyncSession) -> None:
        # Only the stream's own teardown commit parks: nothing else commits
        # between its subscribe and its response.
        if hub.subscriber_count > subscribed_before and not parked.is_set():
            parked.set()
            await asyncio.Event().wait()
        await commit(self)

    monkeypatch.setattr(settings, "realtime_sse_max_stream_seconds", HELD_STREAM_SECONDS)
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", HELD_STREAM_SECONDS)
    monkeypatch.setattr(AsyncSession, "commit", commit_parking_the_stream)
    task = asyncio.create_task(client.get(STREAM, **kwargs))
    try:
        await _until(parked.is_set, what="the stream request reaches its teardown commit")
        yield task
    finally:
        monkeypatch.setattr(AsyncSession, "commit", commit)
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_client_gone_before_the_body_starts_gives_the_slot_back(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot was taken by the route, and the body generator it handed the
    response is what gave it back. A request cancelled before the body ever
    ran left that generator unstarted — its cleanup never runs — so the slot
    stayed taken for the life of the process, and the person's next tab was
    refused with 429 for a stream that no longer existed."""
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    gate = ConnectionGate.sse()
    hub = _hub(realtime_app)
    subscribers = hub.subscriber_count
    async with parked_before_the_body(realtime_app, client, monkeypatch) as task:
        assert gate.active == 1, "the parked request holds its slot"
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # Nothing has to arrive for the slot to come back; the bound only
        # covers the scheduling of the cancelled request's own teardown.
        await _until(lambda: gate.active == 0, 1.0, what="the cancelled request frees its slot")
        assert hub.subscriber_count == subscribers, "and drops its subscription"
    async with _held_stream(realtime_app, client, monkeypatch):
        assert gate.active == 1


async def _box_client(
    app: FastAPI, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> tuple[AsyncClient, dict[str, str]]:
    """A box the person registered: a client on the box's own session token,
    and the assertion naming the machine registered on it."""
    from tests.files._boxes import registered_box

    token, machine_id = await registered_box(
        real_session,
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_id=org_admin.org_id,
    )
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Origin": settings.frontend_base_url, "Authorization": f"Bearer {token}"},
    )
    return client, agent_headers(machine_id)


@pytest.mark.compute_rows
async def test_a_box_stream_is_admitted_behind_its_owner_full_cap(
    client: AsyncClient,
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The box opens its event stream with its own session plus the agent
    assertion naming the machine registered on it. Counted under the owner,
    that one stream met the owner's cap together with their tabs and the tabs
    lost their live updates; it is counted under the machine instead, with
    slots of its own -- so a second stream from the same machine is what meets
    the box's cap, and the owner's slot is never the box's to take (the same
    key decides both).

    One client per stream: a held stream is ended by dropping its client."""
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    box, asserted = await _box_client(realtime_app, real_session, org_admin)
    as_box = {"headers": asserted}
    async with aclosing(box):
        async with _held_stream(realtime_app, client, monkeypatch) as owner:
            assert (await _refused(client)).status_code == 429
            async with _held_stream(realtime_app, box, monkeypatch, **as_box) as held:
                assert not owner.done() and not held.done()
                assert ConnectionGate.sse().active == 2
                assert (await _refused(box, **as_box)).status_code == 429


async def test_a_new_made_up_machine_id_per_stream_buys_no_slot(
    client: AsyncClient,
    realtime_app: FastAPI,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assertion is two headers any member can write. Taken at its word, a
    fresh id per stream was a fresh per-user cap every time, and one member
    could fill the process's streams for every tenant on the replica. An id
    that does not verify as a machine the member registered on this session is
    no machine: the stream is the member's, on the member's cap."""
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with _held_stream(realtime_app, client, monkeypatch, headers=agent_headers(str(uuid4()))):
        for made_up in (str(uuid4()), uuid4().hex, "sess-box-b"):
            refused = await _refused(client, headers=agent_headers(made_up))
            assert refused.status_code == 429, made_up
            assert refused.json()["error"]["code"] == "too_many_streams"
        assert ConnectionGate.sse().active == 1


@pytest.mark.compute_rows
async def test_every_machine_of_one_person_shares_the_per_person_cap(
    client: AsyncClient,
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each verified box has slots of its own, and the person's own tabs are
    never the box's to take -- but together they stop at the per-person cap,
    however many boxes the person registers."""
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_principal", 2)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    box_a, as_a = await _box_client(realtime_app, real_session, org_admin)
    box_b, as_b = await _box_client(realtime_app, real_session, org_admin)
    async with aclosing(box_a), aclosing(box_b):
        async with _held_stream(realtime_app, client, monkeypatch):
            async with _held_stream(realtime_app, box_a, monkeypatch, headers=as_a):
                refused = await _refused(box_b, headers=as_b)
                assert refused.status_code == 429
                assert refused.json()["error"]["code"] == "too_many_streams"
            # The first box's slot came back: the second box now has room.
            async with _held_stream(realtime_app, box_b, monkeypatch, headers=as_b):
                assert ConnectionGate.sse().active == 2


async def test_the_process_cap_counts_every_user(
    realtime_app: FastAPI,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_sse_max_streams", 1)
    monkeypatch.setattr(settings, "realtime_sse_max_streams_per_user", 1)
    _a, a_client = await _member_client(realtime_app, real_session, org_admin.org_id)
    _b, b_client = await _member_client(realtime_app, real_session, org_admin.org_id)
    async with aclosing(a_client), aclosing(b_client):
        async with _held_stream(realtime_app, a_client, monkeypatch) as task:
            refused = await _refused(b_client)
            assert not task.done(), (
                "the refusal was answered while the other user's stream was open"
            )
            assert refused.status_code == 429
        assert (await b_client.get(STREAM)).status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI contract
# ---------------------------------------------------------------------------


def test_openapi_documents_the_stream_and_carries_the_event_vocabulary(
    realtime_app: FastAPI,
) -> None:
    from alkera_core.events import RealtimeEventType

    spec = realtime_app.openapi()
    op = spec["paths"][STREAM]["get"]
    ok = op["responses"]["200"]["content"]
    assert ok["text/event-stream"]["schema"] == {"type": "string"}
    assert ok["application/json"]["schema"] == {"$ref": "#/components/schemas/SseEventData"}
    assert "429" in op["responses"]
    params = {(p["name"], p["in"]) for p in op["parameters"]}
    assert params == {("Last-Event-ID", "header"), ("after", "query")}
    assert not {name for name, where in params if where == "query"} & {"token", "ticket"}
    body = spec["components"]["schemas"]["SseEventData"]
    assert set(body["required"]) == {"type", "entity", "entity_id", "org_id"}
    assert body["properties"]["type"] == {"$ref": "#/components/schemas/RealtimeEventType"}
    enum = spec["components"]["schemas"]["RealtimeEventType"]["enum"]
    assert set(enum) == {m.value for m in RealtimeEventType}
    assert "doc.op" not in enum and "authz.decision" not in enum
