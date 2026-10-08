"""The LISTEN loop against a real Postgres with a real LISTEN: delivery,
no replay of history, catch-up across a stop, reconnect after the backend is
killed server-side, the poll fallback, out-of-order stragglers, the ephemeral
lane, overflow, metrics, and a shutdown that closes what it opened.

Every listener here gets an injected connect factory so the test can reach the
raw connection it made — its backend pid, whether it was closed — without a
test-only hook on the listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import (
    EventHub,
    HubEvent,
    PgListener,
    ResetMarker,
    Subscription,
    asyncpg_dsn,
    emit,
    ephemeral_payload,
    parse_ephemeral,
)
from alkera_core.events import listener as listener_module
from alkera_core.events.listener import (
    BACKOFF_CAP_SECONDS,
    BACKOFF_JITTER_SECONDS,
    EPHEMERAL_CHANNEL,
    backoff_seconds,
)
from prometheus_client import REGISTRY
from sqlalchemy import text

# An upper limit sized for a loaded CI/xdist host; the 30 s poll interval is what proves NOTIFY.
DELIVERY_TIMEOUT = 20.0


class _Connections:
    """A connect factory that keeps every connection it made and can be told to
    fail its first attempts."""

    def __init__(
        self, *, wrap: Callable[[asyncpg.Connection], Any] | None = None, fail_first: int = 0
    ):
        self.made: list[asyncpg.Connection] = []
        self.attempts = 0
        self._wrap = wrap
        self._fail_first = fail_first

    async def __call__(self) -> Any:
        self.attempts += 1
        if self.attempts <= self._fail_first:
            raise ConnectionRefusedError(f"simulated connect failure #{self.attempts}")
        conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
        self.made.append(conn)
        return self._wrap(conn) if self._wrap is not None else conn


class _Deaf:
    """A connection whose LISTEN is a no-op: no notification ever arrives, so
    only the poll can deliver."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def add_listener(self, channel: str, callback: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _Delayed:
    """A connection that hands notifications over late, so the poll reads a
    row by cursor before its own doorbell arrives."""

    delay = 0.6

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def add_listener(self, channel: str, callback: Any) -> None:
        loop = asyncio.get_running_loop()

        def late(*args: Any) -> None:
            loop.call_later(self.delay, callback, *args)

        await self._conn.add_listener(channel, late)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _quick(_seconds: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(listener_module, "_sleep", _quick)


@pytest.fixture
def hub() -> EventHub:
    return EventHub()


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[Callable[..., _Connections]]:
    built: list[_Connections] = []

    def build(**kwargs: Any) -> _Connections:
        connections = _Connections(**kwargs)
        built.append(connections)
        return connections

    yield build
    for connections in built:
        for conn in connections.made:
            if not conn.is_closed():
                conn.terminate()


@contextlib.asynccontextmanager
async def _running(listener: PgListener) -> AsyncIterator[PgListener]:
    await listener.start()
    try:
        yield listener
    finally:
        await listener.stop()


def _for_org(hub: EventHub, org: Any, **kwargs: Any) -> Subscription:
    return hub.subscribe(lambda e: e.org_id == org, **kwargs)


async def _next(sub: Subscription, seconds: float = DELIVERY_TIMEOUT) -> Any:
    return await asyncio.wait_for(sub.queue.get(), timeout=seconds)


async def _nothing(sub: Subscription, seconds: float = 0.5) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sub.queue.get(), timeout=seconds)


async def _until(predicate: Callable[[], bool], seconds: float = DELIVERY_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not predicate():
        if loop.time() > deadline:
            pytest.fail("condition not met in time")
        await asyncio.sleep(0.02)


async def _emit(org: Any, entity_id: str = "e", **kwargs: Any) -> int:
    async with AsyncSessionLocal() as session:
        row = await emit(
            session,
            org_id=org,
            type="kb_item.changed",
            entity="kb_item",
            entity_id=entity_id,
            **kwargs,
        )
        await session.commit()
        return row.id


async def _notify(channel: str, payload: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload}
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_notify_reaches_the_hub_with_the_row(hub: EventHub, factory: Any) -> None:
    # A long poll so a delivery inside the timeout can only have come from NOTIFY.
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        row_id = await _emit(org, "n", version=2, payload={"team_id": str(org)})
        event = await _next(sub)
        assert event == HubEvent(
            lane="durable",
            org_id=org,
            type="kb_item.changed",
            entity="kb_item",
            entity_id="n",
            version=2,
            visibility="org",
            payload={"team_id": str(org)},
            id=row_id,
            channel=None,
        )
        assert event.team_id == org
        assert listener.last_seen_id >= row_id


async def test_history_before_the_first_connect_is_not_replayed(
    hub: EventHub, factory: Any
) -> None:
    """A fresh listener starts at the head: a process booting against a large
    outbox must not fan out the whole table."""
    org = uuid4()
    await _emit(org, "old-1")
    await _emit(org, "old-2")
    listener = PgListener(hub, connect=factory(), poll_interval=0.2)
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        await _nothing(sub, 0.5)
        new_id = await _emit(org, "new")
        assert (await _next(sub)).entity_id == "new"
        assert listener.last_seen_id == new_id


async def test_catch_up_delivers_rows_emitted_while_stopped_in_id_order(
    hub: EventHub, factory: Any
) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org)
    await listener.start()
    assert await listener.wait_connected(DELIVERY_TIMEOUT)
    await listener.stop()
    ids = [await _emit(org, f"while-down-{i}") for i in range(3)]
    async with _running(listener):
        received = [await _next(sub) for _ in range(3)]
        assert [e.id for e in received] == ids
        assert [e.entity_id for e in received] == [f"while-down-{i}" for i in range(3)]
        await _nothing(sub)


async def test_catch_up_pages_through_more_rows_than_one_batch(hub: EventHub, factory: Any) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30, catch_up_batch=2)
    org = uuid4()
    sub = _for_org(hub, org, maxsize=32)
    await listener.start()
    assert await listener.wait_connected(DELIVERY_TIMEOUT)
    await listener.stop()
    ids = [await _emit(org, f"p{i}") for i in range(5)]
    async with _running(listener):
        received = [await _next(sub) for _ in range(5)]
        assert [e.id for e in received] == ids
        await _nothing(sub)


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


async def test_reconnects_after_the_backend_is_terminated(hub: EventHub, factory: Any) -> None:
    connections = factory()
    listener = PgListener(hub, connect=connections, poll_interval=0.5)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        first = connections.made[0]
        async with AsyncSessionLocal() as session:
            killed = (
                await session.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": first.get_server_pid()}
                )
            ).scalar_one()
        assert killed is True
        await _until(lambda: len(connections.made) >= 2 and listener.connected)
        assert first.is_closed()
        row_id = await _emit(org, "after-kill")
        assert (await _next(sub)).id == row_id


async def test_poll_fallback_delivers_without_notify(hub: EventHub, factory: Any) -> None:
    listener = PgListener(hub, connect=factory(wrap=_Deaf), poll_interval=0.2)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        row_id = await _emit(org, "polled")
        event = await _next(sub)
        assert event.id == row_id
        assert listener.last_seen_id == row_id


async def test_a_straggler_with_a_lower_id_is_fetched_by_id_exactly_once(
    hub: EventHub, factory: Any
) -> None:
    """Row N is inserted first but commits after N+1. The cursor moved past N
    when N+1 arrived; N's own notification fetches it by id — once."""
    listener = PgListener(hub, connect=factory(), poll_interval=0.3)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        async with AsyncSessionLocal() as a, AsyncSessionLocal() as b:
            low = await emit(
                a, org_id=org, type="kb_item.changed", entity="kb_item", entity_id="low"
            )
            high = await emit(
                b, org_id=org, type="kb_item.changed", entity="kb_item", entity_id="high"
            )
            assert low.id < high.id
            await b.commit()
            assert (await _next(sub)).id == high.id
            assert listener.last_seen_id >= high.id
            await a.commit()
            assert (await _next(sub)).id == low.id
        # At least three polls pass: neither row is published a second time.
        await _nothing(sub, 1.0)


async def test_a_late_notification_for_a_row_already_read_is_not_published_twice(
    hub: EventHub, factory: Any
) -> None:
    """The poll reads a committed row by cursor; its NOTIFY arrives afterwards
    with an id at or below the cursor — the straggler shape — but the row was
    already published, so it must not be published again."""
    listener = PgListener(hub, connect=factory(wrap=_Delayed), poll_interval=0.2)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        row_id = await _emit(org, "once")
        assert (await _next(sub)).id == row_id  # via the poll
        await asyncio.sleep(_Delayed.delay + 0.3)  # the late doorbell has rung by now
        await _nothing(sub, 0.5)


async def test_start_never_raises_when_connect_fails_and_recovers(
    hub: EventHub, factory: Any
) -> None:
    connections = factory(fail_first=3)
    listener = PgListener(hub, connect=connections, poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org)
    await listener.start()  # returns at once, nothing raised
    assert listener.connected is False
    try:
        await _until(lambda: listener.connected)
        assert connections.attempts == 4
        row_id = await _emit(org, "recovered")
        assert (await _next(sub)).id == row_id
    finally:
        await listener.stop()


async def test_stop_closes_the_connection_and_is_idempotent(hub: EventHub, factory: Any) -> None:
    connections = factory()
    listener = PgListener(hub, connect=connections, poll_interval=30)
    await listener.start()
    await listener.start()  # a second start is a no-op
    assert await listener.wait_connected(DELIVERY_TIMEOUT)
    assert listener.running is True
    await listener.stop()
    assert connections.made[0].is_closed()
    assert listener.connected is False
    assert listener.running is False
    await listener.stop()  # idempotent
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        assert len(connections.made) == 2


async def test_listener_connected_gauge_follows_the_connection(hub: EventHub, factory: Any) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        assert REGISTRY.get_sample_value("alkera_realtime_listener_connected") == 1.0
    assert REGISTRY.get_sample_value("alkera_realtime_listener_connected") == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"poll_interval": 0}, id="zero-poll"),
        pytest.param({"poll_interval": -1}, id="negative-poll"),
        pytest.param({"catch_up_batch": 0}, id="zero-batch"),
    ],
)
def test_a_listener_refuses_a_non_positive_interval_or_batch(
    hub: EventHub, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        PgListener(hub, **kwargs)


# ---------------------------------------------------------------------------
# Overflow
# ---------------------------------------------------------------------------


async def test_a_slow_subscriber_gets_a_reset_marker_and_resumes_after_ack(
    hub: EventHub, factory: Any
) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org, maxsize=1)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        async with AsyncSessionLocal() as session:
            for i in range(4):
                await emit(
                    session, org_id=org, type="kb_item.changed", entity="kb_item", entity_id=str(i)
                )
            await session.commit()
        marker = await _next(sub)
        assert marker == ResetMarker(reason="overflow", dropped=2)
        assert sub.queue.empty()
        hub.ack_reset(sub)
        row_id = await _emit(org, "after-reset")
        assert (await _next(sub)).id == row_id


# ---------------------------------------------------------------------------
# Ephemeral lane
# ---------------------------------------------------------------------------


async def test_an_ephemeral_notification_becomes_an_ephemeral_hub_event(
    hub: EventHub, factory: Any
) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org, user = uuid4(), uuid4()
    sub = _for_org(hub, org)
    event = HubEvent(
        lane="ephemeral",
        org_id=org,
        type="doc.op",
        entity="doc",
        entity_id="doc:chat:abc",
        version=0,
        visibility=f"user:{user}",
        payload={"intent": "chunk", "text": "hé"},
        id=None,
        channel="doc:chat:abc",
    )
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        await _notify(EPHEMERAL_CHANNEL, ephemeral_payload(event))
        assert await _next(sub) == event
        await _nothing(sub)  # nothing durable was written, so nothing else arrives


async def test_malformed_ephemeral_payloads_are_dropped_and_the_listener_survives(
    hub: EventHub, factory: Any
) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org)
    good = HubEvent(
        lane="ephemeral",
        org_id=org,
        type="presence",
        entity="doc",
        entity_id="doc:artifact:1",
        version=0,
        visibility="org",
        payload={},
        channel="doc:artifact:1",
    )
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        for bad in (
            "not json",
            "[]",
            json.dumps({"type": "presence"}),
            json.dumps({**json.loads(ephemeral_payload(good)), "org_id": "nope"}),
            json.dumps({**json.loads(ephemeral_payload(good)), "visibility": "team:x"}),
            json.dumps({**json.loads(ephemeral_payload(good)), "version": -1}),
        ):
            await _notify(EPHEMERAL_CHANNEL, bad)
        await _notify(EPHEMERAL_CHANNEL, ephemeral_payload(good))
        assert await _next(sub) == good
        assert listener.connected is True
        await _nothing(sub)


async def test_a_bad_durable_notification_is_dropped_not_fatal(hub: EventHub, factory: Any) -> None:
    listener = PgListener(hub, connect=factory(), poll_interval=30)
    org = uuid4()
    sub = _for_org(hub, org)
    async with _running(listener):
        assert await listener.wait_connected(DELIVERY_TIMEOUT)
        await _notify("alkera_events", "not-an-id")
        row_id = await _emit(org, "still-alive")
        assert (await _next(sub)).id == row_id


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "postgresql+asyncpg://alkera:alkera@localhost:5432/alkera",
            "postgresql://alkera:alkera@localhost:5432/alkera",
            id="plain",
        ),
        pytest.param(
            "postgresql+asyncpg://u:p%40ss@db.internal:6543/app?x=1",
            "postgresql://u:p%40ss@db.internal:6543/app?x=1",
            id="escaped-password-and-query",
        ),
        pytest.param(
            "postgresql://already/plain",
            "postgresql://already/plain",
            id="already-plain",
        ),
    ],
)
def test_asyncpg_dsn_strips_the_driver_and_keeps_the_password(url: str, expected: str) -> None:
    assert asyncpg_dsn(url) == expected


@pytest.mark.parametrize(
    ("attempt", "base"),
    [
        (1, 0.5),
        (2, 1.0),
        (3, 2.0),
        (4, 4.0),
        (5, 8.0),
        (6, BACKOFF_CAP_SECONDS),
        (12, BACKOFF_CAP_SECONDS),
    ],
)
def test_backoff_doubles_to_the_cap_with_bounded_jitter(attempt: int, base: float) -> None:
    for _ in range(20):
        delay = backoff_seconds(attempt)
        assert base <= delay <= base + BACKOFF_JITTER_SECONDS


def test_ephemeral_payload_round_trips() -> None:
    event = HubEvent(
        lane="ephemeral",
        org_id=uuid4(),
        type="doc.op",
        entity="doc",
        entity_id="doc:chat:x",
        version=5,
        visibility="platform",
        payload={"nested": {"a": [1, 2]}},
        channel="doc:chat:x",
    )
    encoded = ephemeral_payload(event)
    assert parse_ephemeral(encoded) == event
    assert len(encoded.encode()) <= settings.realtime_ephemeral_max_bytes


def test_ephemeral_payload_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "realtime_ephemeral_max_bytes", 256)
    small = HubEvent(
        lane="ephemeral",
        org_id=uuid4(),
        type="t",
        entity="e",
        entity_id="i",
        version=0,
        visibility="org",
        payload={"x": "a" * 50},
    )
    assert len(ephemeral_payload(small).encode()) <= 256
    big = HubEvent(
        lane="ephemeral",
        org_id=uuid4(),
        type="t",
        entity="e",
        entity_id="i",
        version=0,
        visibility="org",
        payload={"x": "a" * 300},
    )
    with pytest.raises(ValueError, match="the cap is 256"):
        ephemeral_payload(big)


_VALID = {
    "org_id": "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f",
    "type": "doc.op",
    "entity": "doc",
    "entity_id": "doc:chat:x",
    "version": 0,
    "visibility": "org",
    "payload": {},
    "channel": None,
}


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        pytest.param("{not json", "not JSON", id="not-json"),
        pytest.param("[]", "JSON object", id="list"),
        pytest.param('"str"', "JSON object", id="string"),
        pytest.param(json.dumps({**_VALID, "type": ""}), "'type'", id="empty-type"),
        pytest.param(
            json.dumps({k: v for k, v in _VALID.items() if k != "entity"}),
            "'entity'",
            id="missing-entity",
        ),
        pytest.param(json.dumps({**_VALID, "entity_id": 3}), "'entity_id'", id="entity-id-not-str"),
        pytest.param(
            json.dumps({**_VALID, "visibility": "team:x"}),
            "invalid visibility",
            id="bad-visibility",
        ),
        pytest.param(json.dumps({**_VALID, "org_id": "nope"}), "'org_id'", id="bad-org"),
        pytest.param(
            json.dumps({k: v for k, v in _VALID.items() if k != "org_id"}),
            "'org_id'",
            id="missing-org",
        ),
        pytest.param(json.dumps({**_VALID, "version": -1}), "'version'", id="negative-version"),
        pytest.param(json.dumps({**_VALID, "version": True}), "'version'", id="bool-version"),
        pytest.param(json.dumps({**_VALID, "version": "1"}), "'version'", id="string-version"),
        pytest.param(json.dumps({**_VALID, "payload": "x"}), "'payload'", id="payload-not-object"),
        pytest.param(json.dumps({**_VALID, "channel": 7}), "'channel'", id="channel-not-str"),
    ],
)
def test_parse_ephemeral_rejects_malformed_input(raw: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_ephemeral(raw)


def test_parse_ephemeral_defaults_version_payload_and_channel() -> None:
    minimal = {
        k: v
        for k, v in _VALID.items()
        if k in ("org_id", "type", "entity", "entity_id", "visibility")
    }
    event = parse_ephemeral(json.dumps(minimal))
    assert event.lane == "ephemeral"
    assert event.id is None
    assert event.version == 0
    assert event.payload == {}
    assert event.channel is None
