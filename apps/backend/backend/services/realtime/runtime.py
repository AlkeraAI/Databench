"""What the backend starts for realtime when it boots, and stops when it exits.

One :class:`RealtimeRuntime` per application: an :class:`EventHub` the
connection handlers subscribe to, the :class:`PgListener` that feeds it from
the outbox, and one sweeper task for the housekeeping no request should pay
for (expired presence rows, spent socket tickets). The app's lifespan
owns it; a process that never runs the lifespan (the OpenAPI
export, an in-process test client) has no runtime, and both realtime surfaces
refuse rather than serve a connection that could never receive anything — the
stream with 503, the socket with a close code — as they do for a process whose
listener could never feed one (:func:`can_deliver`).

The hub belongs to the runtime rather than being process-global so two
application instances in one process — the two-instance tests — each fan
out on their own loop; ``asyncio.Queue`` is bound to the loop that first uses
it and a put from another thread's loop would not wake the reader.

``start`` never raises: a database that is down at boot is the listener's
reconnect loop, not a failed process, and a bug here must not stop the HTTP
surface from coming up.

``stop`` drains before it tears down: the runtime stops admitting sockets and
closes every one it holds with ``1012`` (service restart) FIRST, and only then
stops the listener. A socket left open past that point would be one the hub no
longer feeds — open, answering nothing — and a client has no way to tell it
from a quiet one; closed, it reconnects to a replica that can deliver.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass, field
from typing import Final, Protocol

from alkera_core.config import settings
from alkera_core.events import EventHub, PgListener
from alkera_core.files.promotion import Promoter
from alkera_core.logging import get_logger
from fastapi import FastAPI

from backend.services.crdt.docs import CrdtDocs, admission_slots_for
from backend.services.crdt.sandbox.pool import PoolConfig, SandboxPool
from backend.services.notebooks import (
    NotebookService,
    RecordedDecider,
    build_service,
    close_service,
)
from backend.services.realtime import STATE_ATTR, close_codes, transcript_watch

log = get_logger(__name__)

#: Identifies this process on ephemeral payloads (debugging only; nothing
#: routes on it).
INSTANCE_ID: Final[str] = secrets.token_hex(4)

_STATE_ATTR = STATE_ATTR


#: How often the sweeper clears out rows nothing needs any more: presence rows
#: unheard from past the TTL, and consumed socket tickets past their replay
#: window.
SWEEP_INTERVAL_SECONDS = 60.0

#: How long a connection waits for a listener that exists but is not connected
#: yet — a process still booting, or one mid-reconnect — before it is refused.
#: Short on purpose: the honest refusal is cheap, and the client's polling
#: fallback (or another replica) covers the gap until the listener is back.
LISTENER_READY_SECONDS = 2.0


#: How long a draining runtime waits for the sockets it closed to unwind —
#: leave presence, drop their subscriptions — before it stops the listener
#: regardless. Well inside the process's own graceful-shutdown deadline.
DRAIN_SOCKETS_SECONDS = 5.0


class OpenSocket(Protocol):
    """What the runtime needs of a socket it holds: a way to close it."""

    async def close(self, code: int, reason: str) -> None: ...


@dataclass
class RealtimeRuntime:
    hub: EventHub
    listener: PgListener | None
    sweeper: asyncio.Task[None] | None = None
    #: Asks the machine holding a folder for one file's bytes and waits on
    #: ``hub`` for them to land. It lives here because it waits on this hub:
    #: its in-flight requests are this process's, as are its sockets.
    promoter: Promoter | None = None
    #: Tells the registered transcript watchers (``transcript_watch``) that a
    #: chat's transcript moved, off this hub.
    watcher: asyncio.Task[None] | None = None
    #: The Loro CRDT lane's document store and its sandbox workers (started on
    #: first use, so a process nobody co-edits on starts none).
    crdt: CrdtDocs | None = None
    #: The notebook routes' service: its kernel-event feed and caret board
    #: follow this hub.
    notebooks: NotebookService | None = None
    #: The sockets this process is serving, each registered for as long as
    #: its loops run, so a stop can close them before the hub goes quiet.
    #: Keyed by identity: a socket session is a dataclass, not hashable.
    sockets: dict[int, OpenSocket] = field(default_factory=dict)
    #: Set while no socket is held: what a drain waits on.
    no_sockets: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set once the process has begun to stop: no socket is admitted after it.
    draining: bool = False
    stopped: bool = False

    def __post_init__(self) -> None:
        self.no_sockets.set()

    def hold(self, sock: OpenSocket) -> None:
        """Register a socket for as long as its loops run."""
        self.sockets[id(sock)] = sock
        self.no_sockets.clear()

    def release(self, sock: OpenSocket) -> None:
        self.sockets.pop(id(sock), None)
        if not self.sockets:
            self.no_sockets.set()


async def start(application: FastAPI, *, decide: RecordedDecider) -> RealtimeRuntime:
    """Build the runtime, start the listener when enabled and the sweeper, and
    bind it to ``application.state``. ``decide`` is how the notebook service
    decides and records what arrives on a socket (``decide_on_record``, handed
    in by the app, since the services sit below authz). Never raises."""
    hub = EventHub()
    listener: PgListener | None = None
    if settings.realtime_listener_enabled:
        try:
            listener = PgListener(hub)
            await listener.start()
        except Exception as exc:  # the HTTP surface must come up regardless
            log.error("realtime.runtime.listener_failed", error=str(exc))
            listener = None
    else:
        log.info("realtime.runtime.listener_disabled")
    sweeper = asyncio.create_task(
        _sweep_forever(SWEEP_INTERVAL_SECONDS), name="alkera-realtime-sweeper"
    )
    runtime = RealtimeRuntime(
        hub=hub,
        listener=listener,
        sweeper=sweeper,
        promoter=Promoter(hub),
        watcher=transcript_watch.start(hub),
        crdt=CrdtDocs(
            pool=SandboxPool(PoolConfig.from_settings()),
            validate_budget=settings.realtime_crdt_validate_timeout_ms / 1000,
            load_budget=settings.realtime_crdt_load_timeout_ms / 1000,
            admission_slots=admission_slots_for(pool_size=settings.database_pool_size),
        ),
    )
    if runtime.crdt is not None:
        # Every worker warms before it serves; started now, the first edit
        # after a deploy finds them ready.
        runtime.crdt.pool.start()
    runtime.notebooks = build_service(hub, getattr(runtime.crdt, "notebooks", None), decide=decide)
    setattr(application.state, _STATE_ATTR, runtime)
    if runtime.crdt is not None and settings.realtime_crdt_unsaved_sweep_enabled:
        # Edits a stopped process left unwritten are written back now, not
        # when somebody next types.
        runtime.crdt.start_sweeper()
    log.info("realtime.runtime.started", instance=INSTANCE_ID, listener=listener is not None)
    return runtime


async def _sweep_forever(interval: float) -> None:
    """Delete presence rows past the TTL and consumed socket tickets past their
    replay window, forever; a failure is logged and the loop continues (a sweep
    is housekeeping, never a reason to stop serving).

    The ticket purge lives here rather than on the handshake, which every
    socket runs: housekeeping for a whole table does not belong in front of a
    connection."""
    from alkera_core.db.session import AsyncSessionLocal

    from backend.services.realtime import presence, tickets

    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as db:
                swept = await presence.sweep_expired(db)
                purged = await tickets.purge_consumed(db)
                await db.commit()
            if swept:
                log.info("realtime.presence.swept", rows=swept)
            if purged:
                log.info("realtime.tickets.purged", rows=purged)
        except Exception as exc:  # keep sweeping
            log.warning("realtime.presence.sweep_failed", error=str(exc))


async def drain_sockets(runtime: RealtimeRuntime) -> int:
    """Admit no further socket, close every open one with ``1012`` (service
    restart), and wait (bounded) for them to unwind. Returns how many were
    closed. Idempotent."""
    runtime.draining = True
    held = list(runtime.sockets.values())
    if held:
        await asyncio.gather(
            *(sock.close(close_codes.SERVICE_RESTART, "server restarting") for sock in held),
            return_exceptions=True,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(runtime.no_sockets.wait(), DRAIN_SOCKETS_SECONDS)
        log.info(
            "realtime.runtime.sockets_drained", closed=len(held), remaining=len(runtime.sockets)
        )
    return len(held)


async def stop(application: FastAPI, runtime: RealtimeRuntime) -> None:
    """Drain the sockets, then stop the sweeper and the listener and unbind the
    runtime so a later ``start`` on the same application object begins clean.

    The sockets go first, while the hub still feeds them: under uvicorn a
    SIGTERM has already closed them by the time the lifespan ends, but any
    socket still open here — a server that ends the lifespan first, a
    connection that outlived the graceful deadline — would otherwise stay open
    on a process that can no longer deliver to it. Idempotent."""
    await drain_sockets(runtime)
    if runtime.stopped:
        return
    runtime.stopped = True
    if runtime.sweeper is not None:
        runtime.sweeper.cancel()
        with contextlib.suppress(BaseException):
            await runtime.sweeper
    await transcript_watch.stop(runtime.watcher)
    await close_service(runtime.notebooks)
    if runtime.crdt is not None:
        try:
            await runtime.crdt.aclose()
            await runtime.crdt.pool.close()
        except Exception as exc:  # shutdown must finish
            log.warning("realtime.runtime.crdt_stop_failed", error=str(exc))
    if runtime.promoter is not None:
        await runtime.promoter.close()
    if runtime.listener is not None:
        try:
            await runtime.listener.stop()
        except Exception as exc:  # shutdown must finish
            log.warning("realtime.runtime.listener_stop_failed", error=str(exc))
    if getattr(application.state, _STATE_ATTR, None) is runtime:
        setattr(application.state, _STATE_ATTR, None)
    log.info("realtime.runtime.stopped", instance=INSTANCE_ID)


async def promoter_for(application: FastAPI) -> Promoter | None:
    """The promoter a request on ``application`` may wait on, or ``None`` when
    this process could not hear the answer: no runtime, or a listener that
    cannot deliver another replica's frames. A reader on such a replica gets
    the store's answer at once rather than a wait that could only time out."""
    runtime = runtime_of(application)
    if runtime is None or runtime.promoter is None:
        return None
    if not await can_deliver(runtime):
        return None
    return runtime.promoter


async def can_deliver(runtime: RealtimeRuntime) -> bool:
    """Whether a connection served by this process could carry an event at all.

    Every frame from another replica — an event-stream row, a ``doc.op``, a
    presence change — reaches this process only through the outbox listener
    that feeds the hub. A process built without one (the setting off, or a
    listener that failed at boot) will never deliver, so it is refused at once;
    one whose listener is merely not connected yet gets a short wait first, so
    a connection arriving during boot or a reconnect is not refused for a blip.

    Both realtime surfaces ask this one question, so a client is never told the
    replica is healthy on one and unusable on the other: the event stream
    answers ``503``, the socket closes ``UNAVAILABLE``."""
    listener = runtime.listener
    if listener is None:
        return False
    return listener.connected or await listener.wait_connected(LISTENER_READY_SECONDS)


def runtime_of(application: FastAPI) -> RealtimeRuntime | None:
    """The runtime bound to ``application``, or ``None`` before the lifespan
    started it (or after it stopped)."""
    runtime = getattr(application.state, _STATE_ATTR, None)
    return runtime if isinstance(runtime, RealtimeRuntime) else None


__all__ = [
    "DRAIN_SOCKETS_SECONDS",
    "INSTANCE_ID",
    "LISTENER_READY_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "OpenSocket",
    "RealtimeRuntime",
    "can_deliver",
    "drain_sockets",
    "promoter_for",
    "runtime_of",
    "start",
    "stop",
]
