"""Bytes on demand: ask the machine holding a folder for one file, and wait.

A row under a live lease can exist before its bytes do: the holder reports its
tree ahead of its uploads, and a clone's thousand files trail it by however
long the upload backlog takes. A reader who opens one of them should not wait
for that backlog. :class:`Promoter` asks the holder for that one file — a
``machine.request`` on the machine's own channel — and waits for its bytes to
land through the ordinary fenced upload, which is the only way bytes ever
reach the drive: nothing on the request path carries a byte of the file.

The promoter never touches a socket and holds no database connection while it
waits. It publishes the request on the ephemeral lane (``pg_notify``, one short
transaction of its own), so whichever replica holds the machine's socket
forwards it; then it waits on this process's :class:`EventHub` for one of two
things:

* a ``machine.ack`` naming its request — ``accepted`` keeps it waiting for the
  bytes, anything else is the answer;
* the durable ``file_node.changed`` the content commit writes when the holder's
  own bytes land (``reason: live_saved``) — the answer is ``landed``.

Readers of one file share one request: a second reader of the same node joins
the wait the first one started instead of asking the machine again. A folder's
machine is asked at most ``files_promote_per_minute`` times a minute from one
process; past that a reader is told ``throttled`` at once.

Every wait has a deadline the caller chose, and a machine that has not answered
at all within ``files_promote_ack_seconds`` ends the wait early: a box that is
not listening costs a reader two seconds, not eight.

The hub, the ephemeral lane, the frame models and the logger are imported on
first use: they pull the observability stack through the events barrel, and a
Files module is imported by the CLI and the daemon, which must not pay for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy import text

from alkera_core.config import Settings, get_settings
from alkera_core.files.lease_snapshots import HeldLease, LeaseFacet
from alkera_core.models.files.tree import FileNode

if TYPE_CHECKING:
    from alkera_core.events.hub import EventHub, HubEvent, ResetMarker, Subscription
    from alkera_core.schemas.realtime.machine import (
        LiveTextRequest,
        MachineAck,
        MachineRequest,
        NotebookMachineRequest,
    )

#: The entity every machine-channel event names; its id is the machine's.
MACHINE_ENTITY: Final = "machine"
#: The ``t`` of the two machine-channel frames, spelled as the frame models
#: spell them (a test holds the two spellings together).
MACHINE_REQUEST_TAG: Final = "machine.request"
MACHINE_ACK_TAG: Final = "machine.ack"
#: The durable event a landing arrives as, and the audience every machine event
#: is addressed to (the channel, not the audience, is what confines it).
FILE_NODE_CHANGED: Final = "file_node.changed"
VISIBILITY_ORG: Final = "org"
#: The reason the content commit stamps on the holder's own bytes landing.
LIVE_SAVED: Final = "live_saved"
#: The window the per-folder cap counts over.
RATE_WINDOW_SECONDS: Final = 60.0
#: A flight's own queue: it only ever holds the few events naming it.
FLIGHT_QUEUE_SIZE: Final = 32


class PromoteOutcome(StrEnum):
    """What a promotion came to, from the reader's side."""

    #: The holder's bytes landed; the store now has them.
    LANDED = "landed"
    #: The machine took the request and the bytes were still on their way when
    #: the reader's deadline passed.
    ACCEPTED = "accepted"
    #: The machine never answered within the ack window (or no replica holds
    #: its socket).
    TIMED_OUT = "timed_out"
    #: The folder's lease is not live, or its holder stopped beating: nothing
    #: was asked.
    OFFLINE = "offline"
    #: This folder's machine was asked too often this minute: nothing was asked.
    THROTTLED = "throttled"
    #: The machine answered: not its folder any more, no such file, a file that
    #: changed since it was reported, or too much in flight.
    NOT_HOLDER = "not_holder"
    MISSING = "missing"
    CHANGED = "changed"
    BUSY = "busy"
    #: The reader IS the machine holding the folder: the bytes it asks for are
    #: its own, still unsent, so nothing was asked — a request waiting on its
    #: own sender would only wait out the deadline.
    REQUESTER = "requester"


#: A machine's non-``accepted`` answers, as outcomes.
_ACK_OUTCOMES: Final[dict[str, PromoteOutcome]] = {
    "not_holder": PromoteOutcome.NOT_HOLDER,
    "missing": PromoteOutcome.MISSING,
    "changed": PromoteOutcome.CHANGED,
    "busy": PromoteOutcome.BUSY,
    #: A flush's push ended with everything the machine held of the folder
    #: sent: its bytes have landed, as a promote's landing says of one file.
    "flushed": PromoteOutcome.LANDED,
}

Publish = Callable[["HubEvent"], Awaitable[None]]


def _log() -> Any:
    # Files reaches no first-party module outside its allowlist, so the logger
    # comes from structlog itself; the process's configuration still applies.
    return structlog.get_logger(__name__)


def machine_event(
    org_id: uuid.UUID,
    machine_id: str,
    frame: MachineRequest | LiveTextRequest | NotebookMachineRequest | MachineAck,
) -> HubEvent:
    """The ephemeral hub event a machine-channel frame travels as between
    replicas. The channel is the machine's, so the one socket subscribed to it
    is the only one that forwards a request; the payload is the frame itself."""
    from alkera_core.events.hub import HubEvent
    from alkera_core.schemas.realtime.machine import machine_channel

    return HubEvent(
        lane="ephemeral",
        org_id=org_id,
        type=frame.t,
        entity=MACHINE_ENTITY,
        entity_id=machine_id,
        version=0,
        visibility=VISIBILITY_ORG,
        payload=frame.model_dump(mode="json"),
        id=None,
        channel=machine_channel(machine_id),
    )


async def notify(event: HubEvent) -> None:
    """Publish ``event`` on the ephemeral lane in a transaction of its own.

    The connection is borrowed for the one ``pg_notify`` and handed back at
    commit, which is when Postgres delivers the notification to every
    replica's listener, this one included."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.events.listener import EPHEMERAL_CHANNEL, ephemeral_payload

    encoded = ephemeral_payload(event)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": EPHEMERAL_CHANNEL, "payload": encoded},
        )
        await db.commit()


@dataclass(eq=False)
class _Flight:
    """One request in flight for one file, and everyone waiting on it."""

    key: tuple[uuid.UUID, uuid.UUID]
    request_id: uuid.UUID
    org_id: uuid.UUID
    machine: str
    node_id: uuid.UUID
    #: Loop time the flight gives up at; a later joiner with a longer deadline
    #: moves it out.
    expires: float
    done: asyncio.Future[PromoteOutcome]
    #: Set once the machine answered ``accepted``.
    accepted: bool = False
    task: asyncio.Task[None] | None = None
    #: ``promote`` waits for the file's bytes to land; ``flush`` for the
    #: machine's own word that its push of the folder has ended.
    kind: str = "promote"

    def wants(self, event: HubEvent) -> bool:
        if event.org_id != self.org_id:
            return False
        if event.lane == "durable":
            if self.kind == "flush":
                return False
            return (
                event.type == FILE_NODE_CHANGED
                and event.entity_id == str(self.node_id)
                and event.payload.get("reason") == LIVE_SAVED
            )
        return (
            event.type == MACHINE_ACK_TAG
            and event.entity == MACHINE_ENTITY
            and event.entity_id == self.machine
            and event.payload.get("request_id") == str(self.request_id)
        )


@dataclass
class _Window:
    stamps: deque[float] = field(default_factory=deque)


class Promoter:
    """One per process, beside the hub it waits on. See the module docstring.

    ``publish`` is how a request leaves this process (the ephemeral lane by
    default); ``clock`` drives the per-folder cap only — every wait runs on the
    event loop's own clock, so a test can pin the cap without freezing a wait.
    """

    def __init__(
        self,
        hub: EventHub,
        *,
        publish: Publish = notify,
        clock: Callable[[], float] = time.monotonic,
        settings: Settings | None = None,
    ) -> None:
        self._hub = hub
        self._publish = publish
        self._clock = clock
        self._settings = settings
        self._flights: dict[tuple[uuid.UUID, uuid.UUID], _Flight] = {}
        self._windows: dict[uuid.UUID, _Window] = {}

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    @property
    def in_flight(self) -> int:
        return len(self._flights)

    async def promote(
        self,
        node: FileNode,
        lease: LeaseFacet,
        *,
        deadline: float,
        path: str,
    ) -> PromoteOutcome:
        """Ask ``lease``'s machine for ``node``'s bytes and wait up to
        ``deadline`` seconds for them to land.

        ``path`` is the node's path relative to the lease root, spelled the way
        the holder's tree report spelled it. The node's holder facet is what the
        machine is told to expect; a node with none has nothing to promote.
        """
        if not lease.live or lease.served != "live":
            return PromoteOutcome.OFFLINE
        if node.holder_size is None:
            return PromoteOutcome.MISSING
        loop = asyncio.get_running_loop()
        until = loop.time() + max(deadline, 0.0)
        key = (uuid.UUID(str(lease.node_id)), node.id)
        flight = self._flights.get(key)
        if flight is None:
            if not self._admit(key[0]):
                return PromoteOutcome.THROTTLED
            flight = self._launch(node, lease, key=key, until=until, path=path)
        else:
            flight.expires = max(flight.expires, until)
        return await self._await(flight, until)

    async def flush(self, holder: HeldLease, *, deadline: float) -> PromoteOutcome:
        """Ask ``holder``'s machine to push everything it holds of the folder,
        and wait up to ``deadline`` seconds for it to say the push has ended.

        What a trash over a held folder waits on, so the box's unsent work is
        on the drive (and in the trash with the rest) before the folder goes.
        ``landed`` once the machine says the push ended with everything sent; a
        machine that does not answer within the ack window ends the wait as a
        promote's does: ``timed_out``. Callers asking about one folder at once
        share one request."""
        loop = asyncio.get_running_loop()
        until = loop.time() + max(deadline, 0.0)
        key = (holder.lease_node_id, holder.lease_node_id)
        flight = self._flights.get(key)
        if flight is None:
            from alkera_core.schemas.realtime.machine import MachineRequest

            flight = _Flight(
                key=key,
                request_id=uuid.uuid4(),
                org_id=holder.org_id,
                machine=holder.machine,
                node_id=holder.lease_node_id,
                expires=until,
                done=loop.create_future(),
                kind="flush",
            )
            request = MachineRequest(
                request_id=flight.request_id,
                kind="flush",
                lease_node_id=holder.lease_node_id,
                epoch=holder.epoch,
                deadline_ms=max(int(deadline * 1000), 0),
            )
            self._flights[key] = flight
            flight.task = loop.create_task(self._fly(flight, request), name="files-flush")
        else:
            flight.expires = max(flight.expires, until)
        return await self._await(flight, until)

    async def close(self) -> None:
        """Cancel every flight; each waiter is answered ``timed_out``."""
        flights = list(self._flights.values())
        for flight in flights:
            if flight.task is not None:
                flight.task.cancel()
        for flight in flights:
            if flight.task is not None:
                with contextlib.suppress(BaseException):
                    await flight.task

    # -- the cap -----------------------------------------------------------

    def _admit(self, lease_node_id: uuid.UUID) -> bool:
        now = self._clock()
        window = self._windows.setdefault(lease_node_id, _Window())
        cutoff = now - RATE_WINDOW_SECONDS
        while window.stamps and window.stamps[0] <= cutoff:
            window.stamps.popleft()
        # Forget folders nobody asked about for a whole window, so the map is
        # bounded by the folders asked about in the last minute.
        for other, stale in list(self._windows.items()):
            if other != lease_node_id and (not stale.stamps or stale.stamps[-1] <= cutoff):
                del self._windows[other]
        if len(window.stamps) >= max(self.settings.files_promote_per_minute, 0):
            return False
        window.stamps.append(now)
        return True

    # -- a flight ----------------------------------------------------------

    def _launch(
        self,
        node: FileNode,
        lease: LeaseFacet,
        *,
        key: tuple[uuid.UUID, uuid.UUID],
        until: float,
        path: str,
    ) -> _Flight:
        from alkera_core.schemas.realtime.machine import FileStamp, MachineRequest

        loop = asyncio.get_running_loop()
        flight = _Flight(
            key=key,
            request_id=uuid.uuid4(),
            org_id=node.org_team_id,
            machine=lease.machine,
            node_id=node.id,
            expires=until,
            done=loop.create_future(),
        )
        request = MachineRequest(
            request_id=flight.request_id,
            kind="promote",
            lease_node_id=key[0],
            epoch=lease.epoch,
            node_id=node.id,
            path=path,
            expected=FileStamp(size=node.holder_size or 0, mtime_ns=node.holder_mtime_ns or 0),
            deadline_ms=max(int((until - loop.time()) * 1000), 0),
        )
        self._flights[key] = flight
        flight.task = loop.create_task(self._fly(flight, request), name="files-promote")
        return flight

    async def _fly(self, flight: _Flight, request: MachineRequest) -> None:
        """Publish the request, then read what names it until an answer or the
        flight's deadline. Subscribed BEFORE the publish, so an ack or a landing
        that races the notification is never missed."""
        loop = asyncio.get_running_loop()
        sub = self._hub.subscribe(
            flight.wants, label=f"promote:{flight.node_id}", maxsize=FLIGHT_QUEUE_SIZE
        )
        outcome = PromoteOutcome.TIMED_OUT
        try:
            await self._publish(machine_event(flight.org_id, flight.machine, request))
            answer_by = loop.time() + max(self.settings.files_promote_ack_seconds, 0.0)
            while True:
                limit = flight.expires if flight.accepted else min(flight.expires, answer_by)
                remaining = limit - loop.time()
                if remaining <= 0:
                    outcome = (
                        PromoteOutcome.ACCEPTED if flight.accepted else PromoteOutcome.TIMED_OUT
                    )
                    break
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
                except TimeoutError:
                    continue
                answered = self._read(flight, sub, item)
                if answered is not None:
                    outcome = answered
                    break
        except asyncio.CancelledError:
            outcome = PromoteOutcome.TIMED_OUT
        except Exception as exc:  # a publish that failed is a machine never asked
            _log().warning(
                "promotion.machine_unasked",
                node_id=str(flight.node_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            outcome = PromoteOutcome.TIMED_OUT
        finally:
            self._hub.unsubscribe(sub)
            if self._flights.get(flight.key) is flight:
                del self._flights[flight.key]
            if not flight.done.done():
                flight.done.set_result(outcome)
        _log().info(
            "promotion.settled",
            node_id=str(flight.node_id),
            machine=flight.machine,
            outcome=outcome.value,
        )

    def _read(
        self, flight: _Flight, sub: Subscription, item: HubEvent | ResetMarker
    ) -> PromoteOutcome | None:
        """The answer ``item`` gives, or ``None`` to keep waiting."""
        from alkera_core.events.hub import ResetMarker

        if isinstance(item, ResetMarker):
            # Only this flight's own events reach this queue, so an overflow is
            # a burst of repeats; resume and keep reading.
            self._hub.ack_reset(sub)
            return None
        if item.lane == "durable":
            return PromoteOutcome.LANDED
        ack = _ack_of(item.payload)
        if ack is None:
            return None
        if ack.outcome == "accepted":
            flight.accepted = True
            return None
        return _ACK_OUTCOMES[ack.outcome]

    async def _await(self, flight: _Flight, until: float) -> PromoteOutcome:
        remaining = until - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(asyncio.shield(flight.done), timeout=max(remaining, 0))
        except TimeoutError:
            return PromoteOutcome.ACCEPTED if flight.accepted else PromoteOutcome.TIMED_OUT


def _ack_of(payload: dict[str, Any]) -> MachineAck | None:
    from alkera_core.schemas.realtime.machine import MachineAck

    try:
        return MachineAck.model_validate(payload)
    except ValueError:
        return None


__all__ = [
    "FILE_NODE_CHANGED",
    "LIVE_SAVED",
    "MACHINE_ACK_TAG",
    "MACHINE_ENTITY",
    "MACHINE_REQUEST_TAG",
    "VISIBILITY_ORG",
    "PromoteOutcome",
    "Promoter",
    "Publish",
    "machine_event",
    "notify",
]
