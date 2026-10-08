"""One Postgres LISTEN connection per process, feeding the event hub.

:class:`PgListener` holds a single raw asyncpg connection subscribed to two
channels: ``alkera_events``, rung by the outbox trigger with a row id, and
``alkera_rt``, carrying small ephemeral messages (typing chunks, presence)
that never touch a table. It publishes what it learns to an
:class:`~alkera_core.events.hub.EventHub`.

The notification is only a doorbell. On every wake-up — and every
``poll_interval`` seconds without one — the listener reads the outbox by
cursor: first any notified id at or below the cursor (a transaction that
committed after a higher id was already read; fetching it by id is what keeps
connected clients complete), then every row above the cursor in pages. That
read is also what makes a reconnect safe: whatever was committed while the
connection was down is picked up from the cursor, and the poll doubles as a
liveness probe so a dead socket is noticed without waiting for traffic.

A fresh listener starts at the current head and never replays history: the
hub serves clients that are connected now, and each of them resumes from its
own cursor. ``start`` never raises — a database that is down at boot is a
reconnect loop, not a failed process.

Known limit: a row that commits with a LOWER id than one already read, during
a moment the listener was disconnected, is not found by the cursor read (it
looks above the cursor). Connected listeners get it via the notified id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from secrets import SystemRandom
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.tls import asyncpg_connect_args
from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.events.hub import EventHub, HubEvent
from alkera_core.events.outbox import latest_id, read_after, read_ids
from alkera_core.events.types import validate_visibility
from alkera_core.logging import get_logger
from alkera_core.observability.metrics import record_listener_connected

log = get_logger(__name__)

DURABLE_CHANNEL = "alkera_events"
EPHEMERAL_CHANNEL = "alkera_rt"
APPLICATION_NAME = "alkera-realtime-listener"
CONNECT_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 2.0
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 10.0
BACKOFF_JITTER_SECONDS = 0.25
#: How many recently published row ids are remembered so a notified id that
#: was already read by cursor is not published twice.
RECENT_ID_MEMORY = 4096

ConnectFactory = Callable[[], Awaitable[asyncpg.Connection]]

#: The sleep the reconnect loop waits with. A module attribute so a test can
#: make backoff instant without touching the loop.
_sleep = asyncio.sleep
_jitter = SystemRandom()


def asyncpg_dsn(database_url: str) -> str:
    """The SQLAlchemy URL as a plain ``postgresql://`` DSN asyncpg accepts,
    password included."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def default_connect() -> asyncpg.Connection:
    """A raw connection to the configured database, with the same TLS posture
    as the pooled engine and a name an operator can find in ``pg_stat_activity``."""
    return await asyncpg.connect(
        asyncpg_dsn(settings.database_url),
        timeout=CONNECT_TIMEOUT_SECONDS,
        server_settings={"application_name": APPLICATION_NAME},
        **asyncpg_connect_args(),
    )


def backoff_seconds(attempt: int) -> float:
    """Reconnect delay for the ``attempt``-th consecutive failure: doubles from
    half a second, capped at ten, with a little jitter so a fleet that lost the
    database together does not reconnect in lockstep."""
    base = min(BACKOFF_BASE_SECONDS * (2.0 ** max(attempt - 1, 0)), BACKOFF_CAP_SECONDS)
    return base + _jitter.uniform(0, BACKOFF_JITTER_SECONDS)


# ---------------------------------------------------------------------------
# Ephemeral lane wire format
# ---------------------------------------------------------------------------

_EPHEMERAL_STRING_FIELDS = ("type", "entity", "entity_id", "visibility")


def ephemeral_payload(event: HubEvent) -> str:
    """Encode ``event`` for ``pg_notify('alkera_rt', …)``.

    Compact JSON; refused with ``ValueError`` above
    ``settings.realtime_ephemeral_max_bytes`` (Postgres caps a NOTIFY payload
    at 8000 bytes, and the ephemeral lane is for small, disposable messages).
    """
    body = {
        "org_id": str(event.org_id),
        "type": event.type,
        "entity": event.entity,
        "entity_id": respell_channel(event.entity_id, "to_replicas"),
        "version": event.version,
        "visibility": event.visibility,
        "payload": respell_payload(event.payload, "to_replicas"),
        "channel": respell_channel(event.channel, "to_replicas"),
    }
    encoded = json.dumps(body, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > settings.realtime_ephemeral_max_bytes:
        raise ValueError(
            f"ephemeral payload is {size} bytes; the cap is {settings.realtime_ephemeral_max_bytes}"
        )
    return encoded


def parse_ephemeral(raw: str) -> HubEvent:
    """Decode an ``alkera_rt`` notification; ``ValueError`` for anything that
    is not a well-formed ephemeral event."""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ephemeral payload is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("ephemeral payload must be a JSON object")
    fields: dict[str, Any] = {}
    for name in _EPHEMERAL_STRING_FIELDS:
        value = body.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"ephemeral payload field {name!r} must be a non-empty string")
        fields[name] = value
    validate_visibility(fields["visibility"])
    try:
        org_id = UUID(str(body.get("org_id")))
    except ValueError as exc:
        raise ValueError("ephemeral payload field 'org_id' must be a UUID") from exc
    version = body.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("ephemeral payload field 'version' must be a non-negative integer")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("ephemeral payload field 'payload' must be an object")
    channel = body.get("channel")
    if channel is not None and not isinstance(channel, str):
        raise ValueError("ephemeral payload field 'channel' must be a string or null")
    return HubEvent(
        lane="ephemeral",
        org_id=org_id,
        type=fields["type"],
        entity=fields["entity"],
        entity_id=respell_channel(fields["entity_id"], "from_replicas") or fields["entity_id"],
        version=version,
        visibility=fields["visibility"],
        payload=respell_payload(payload, "from_replicas"),
        id=None,
        channel=respell_channel(channel, "from_replicas"),
    )


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class PgListener:
    def __init__(
        self,
        hub: EventHub,
        *,
        connect: ConnectFactory = default_connect,
        session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
        poll_interval: float | None = None,
        catch_up_batch: int = 500,
    ) -> None:
        if catch_up_batch < 1:
            raise ValueError("catch_up_batch must be >= 1")
        self._hub = hub
        self._connect = connect
        self._session_factory = session_factory
        self._poll_interval = (
            settings.realtime_poll_interval_seconds if poll_interval is None else poll_interval
        )
        if self._poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")
        self._catch_up_batch = catch_up_batch
        self._task: asyncio.Task[None] | None = None
        self._conn: asyncpg.Connection | None = None
        self._connected = False
        self._connected_event = asyncio.Event()
        self._lost = False
        self._wake = asyncio.Event()
        self._pending: set[int] = set()
        self._cursor: int | None = None
        self._recent: OrderedDict[int, None] = OrderedDict()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin listening in a background task. Idempotent; never raises — a
        connect failure is the reconnect loop's first attempt."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="alkera-realtime-listener")

    async def stop(self) -> None:
        """Cancel the loop and close the connection. Idempotent. The cursor is
        kept, so a later ``start`` on the same instance resumes where it left off."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        conn, self._conn = self._conn, None
        await self._close(conn)
        self._set_connected(False)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_seen_id(self) -> int:
        return self._cursor or 0

    async def wait_connected(self, seconds: float) -> bool:
        """``True`` once a LISTEN connection is live, ``False`` if ``seconds``
        pass first."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await self._connect()
                self._conn = conn
                self._lost = False
                await conn.add_listener(DURABLE_CHANNEL, self._on_durable)
                await conn.add_listener(EPHEMERAL_CHANNEL, self._on_ephemeral)
                conn.add_termination_listener(self._on_termination)
                # Anything committed while there was no LISTEN is behind the
                # cursor; only once that is read (and, on first contact, the
                # cursor exists at all) is this listener "connected".
                await self._catch_up()
                self._set_connected(True)
                attempt = 0
                log.info("realtime.listener.connected", cursor=self.last_seen_id)
                while True:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                    except TimeoutError:
                        # No doorbell for a whole interval: prove the socket is
                        # alive (a dead one raises here) and read by cursor anyway.
                        await conn.execute("SELECT 1")
                    self._wake.clear()
                    if self._lost or conn.is_closed():
                        raise ConnectionError("listener connection lost")
                    await self._catch_up()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any failure is a reconnect, never a crash
                self._set_connected(False)
                attempt += 1
                log.warning(
                    "realtime.listener.disconnected",
                    error=f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                )
                if self._conn is conn:
                    self._conn = None
                await self._close(conn)
                await _sleep(backoff_seconds(attempt))

    def _on_durable(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            row_id = int(payload)
        except ValueError:
            log.warning("realtime.listener.bad_notify", channel=DURABLE_CHANNEL, payload=payload)
            return
        self._pending.add(row_id)
        self._wake.set()

    def _on_ephemeral(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            event = parse_ephemeral(payload)
        except ValueError as exc:
            log.warning("realtime.listener.ephemeral_dropped", error=str(exc))
            return
        self._hub.publish(event)

    def _on_termination(self, _conn: object) -> None:
        self._lost = True
        self._wake.set()

    async def _catch_up(self) -> None:
        pending, self._pending = self._pending, set()
        async with self._session_factory() as session:
            if self._cursor is None:
                # First contact: start at the head. Nothing before it is replayed.
                self._cursor = await latest_id(session)
                return
            stragglers = sorted(i for i in pending if i <= self._cursor and i not in self._recent)
            if stragglers:
                for row in await read_ids(session, stragglers):
                    self._publish(row)
            while True:
                rows = await read_after(session, after_id=self._cursor, limit=self._catch_up_batch)
                for row in rows:
                    self._publish(row)
                    self._cursor = row.id
                if len(rows) < self._catch_up_batch:
                    break

    def _publish(self, row: Any) -> None:
        if row.id in self._recent:
            return
        self._recent[row.id] = None
        while len(self._recent) > RECENT_ID_MEMORY:
            self._recent.popitem(last=False)
        self._hub.publish(HubEvent.from_outbox(row))

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
        if connected:
            self._connected_event.set()
        else:
            self._connected_event.clear()
        record_listener_connected(connected)

    async def _close(self, conn: asyncpg.Connection | None) -> None:
        if conn is None or conn.is_closed():
            return
        try:
            await asyncio.wait_for(conn.close(), timeout=CLOSE_TIMEOUT_SECONDS)
        except Exception:
            conn.terminate()


__all__ = [
    "APPLICATION_NAME",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "BACKOFF_JITTER_SECONDS",
    "DURABLE_CHANNEL",
    "EPHEMERAL_CHANNEL",
    "RECENT_ID_MEMORY",
    "ConnectFactory",
    "PgListener",
    "asyncpg_dsn",
    "backoff_seconds",
    "default_connect",
    "ephemeral_payload",
    "parse_ephemeral",
]
