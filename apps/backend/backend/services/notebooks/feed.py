"""A notebook kernel's events: accepted from the box, fanned out, replayed.

**In.** The box serving a notebook posts its kernel's events in batches
(:func:`accept_events`). A batch is accepted only from the machine the kernel
is bound to, in the org the kernel belongs to: ``notebook_kernels(kernel_id)``
names both, and a batch whose sender or org differs is refused and goes
nowhere. A kernel the backend has not seen is bound on its first batch, to
the machine holding the notebook's folder and to no other.

**Out.** An accepted batch is one outbox row of type ``notebook.event`` on the
notebook's channel ``nb:<item_id>``, so every replica hears it in commit
order, and the sockets holding the channel forward it.

**The platform's own word.** What the platform itself decides about a run
(a run the box never answered, a request it refused) is announced on the
same channel as an event of no kernel and no sequence
(``alkera_core.notebooks.runs.announce_platform_events``): sockets holding the channel are sent it
as it is, and no ring keeps it.

**Replay.** Each replica keeps, per notebook it has heard of lately, the
kernel's latest ``snapshot`` event and the events after it
(:class:`NotebookFeed`). A socket joining the channel is sent that snapshot at
``(kernel_id, seq)`` and every buffered event after it, then the live ones,
each exactly once (:class:`ChannelCursor`).

**Holes.** The ring is bounded, and a batch can be lost on its way to a
replica. A ring that dropped an event to make room, or heard one past the next
sequence, is no longer whole; a socket that receives one past the next has a
hole too. Either way the box is asked for a snapshot (at most once per
:data:`SNAPSHOT_ASK_SECONDS` while none comes), and the snapshot it posts
brings every socket on the notebook whole again.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.events import EventHub, EventType, HubEvent, emit
from alkera_core.events.hub import Subscription
from alkera_core.events.types import Entity
from alkera_core.logging import get_logger
from alkera_core.notebooks.limits import batches, fit_event
from alkera_core.notebooks.models import NotebookKernel
from alkera_core.notebooks.runs import (
    PLATFORM_EVENTS_KEY,
    item_of_channel,
    nb_channel,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

#: The event type that carries the kernel's whole state at its ``seq``.
SNAPSHOT_EVENT: Final = "snapshot"
#: The event type that offers a widget asset by hash (``{"sha256": ...}``).
ASSET_OFFERED_EVENT: Final = "widget.asset_offered"
#: The kernel's offer of a widget library's files for a module
#: (``{module, version, files: [{path, sha256}]}``).
WIDGET_ASSET_EVENT: Final = "widget.asset"
#: The engine's answer to a request that wants one (``{request_id, result}``
#: or ``{request_id, error: {code, message}}``): taken by the replica waiting
#: for it, never kept or sent to a socket.
ANSWER_EVENT: Final = "answer"
#: The payload key a batch of the platform's own events travels under.
#: The events no socket is sent.
UNSENT_EVENTS: Final = frozenset({ANSWER_EVENT})
#: How many widget modules a replica remembers per kernel.
OFFERED_MODULES: Final = 1024
#: How many events after the latest snapshot a replica keeps per notebook.
RING_EVENTS: Final = 2048
#: How many notebooks a replica keeps a ring for, least recently heard out.
RING_NOTEBOOKS: Final = 512
#: How many widget asset hashes a replica remembers per kernel.
OFFERED_ASSETS: Final = 4096
#: How long a replica waits on a snapshot it asked the box for before it
#: asks again.
SNAPSHOT_ASK_SECONDS: Final = 10.0
#: The most events one batch may carry.
MAX_BATCH_EVENTS: Final = 500
#: The largest sequence an event may carry: a kernel's sequence is the
#: version its batches are announced under, which the outbox keeps as a
#: 32-bit integer.
MAX_EVENT_SEQ: Final = 2**31 - 1
#: The kernel states a batch may report.
KERNEL_STATES: Final = frozenset({"absent", "starting", "idle", "busy", "restarting", "stopped"})
#: The states of a kernel that runs (or is coming up): one notebook has one
#: such kernel at a time, so a kernel reporting one ends every other.
LIVE_KERNEL_STATES: Final = frozenset({"starting", "idle", "busy", "restarting"})
#: The longest environment id a kernel row keeps.
MAX_ENV_ID: Final = 128


class KernelRefusedError(Exception):
    """A batch the backend will not take: from a machine the kernel is not
    bound to, for a kernel of another org or notebook, or for a new kernel
    from a machine that does not hold the notebook's folder."""


@dataclass(frozen=True, slots=True)
class AcceptedBatch:
    """What one accepted batch did: how many events were new (a retried
    batch carries none), and the kernel's sequence after it."""

    kernel_id: str
    accepted: int
    seq: int
    #: The events that were new, in sequence order.
    events: tuple[dict[str, Any], ...] = ()


def seq_out_of_range(events: Iterable[Mapping[str, Any]]) -> int | None:
    """The first integer ``seq`` in ``events`` past :data:`MAX_EVENT_SEQ`
    (or below zero), which no batch may carry; ``None`` when there is none."""
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if seq > MAX_EVENT_SEQ or seq < 0:
            return seq
    return None


def _clean_events(events: Iterable[Mapping[str, Any]], *, after: int) -> list[dict[str, Any]]:
    """The events with a sequence past ``after``, in sequence order, each once.
    An event without a positive integer ``seq`` and a string ``type`` is
    dropped: the box numbers every event it sends."""
    seen: dict[int, dict[str, Any]] = {}
    for event in events:
        seq = event.get("seq")
        kind = event.get("type")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= after:
            continue
        if not isinstance(kind, str) or not kind:
            continue
        seen.setdefault(seq, dict(event))
    return [seen[seq] for seq in sorted(seen)]


def state_of_events(events: Iterable[Mapping[str, Any]]) -> str | None:
    """The kernel state ``events`` leave the kernel in, when they say one:
    the last ``kernel.state``'s, or ``stopped`` after a ``kernel.exited``."""
    state: str | None = None
    for event in events:
        kind = event.get("type")
        if kind == "kernel.state":
            said = event.get("state")
            if isinstance(said, str) and said in KERNEL_STATES:
                state = said
        elif kind == "kernel.exited":
            state = "stopped"
    return state


def env_of_events(events: Iterable[Mapping[str, Any]], kernel_id: str) -> str | None:
    """The environment ``kernel_id`` runs in, as the last of ``events`` that
    says it: a ``kernel.state`` naming its ``env_id``, or a snapshot whose
    kernel is this one and names its environment."""
    env_id: str | None = None
    for event in events:
        kind = event.get("type")
        found: Any = None
        if kind == "kernel.state":
            found = event.get("env_id")
        elif kind == SNAPSHOT_EVENT:
            view = event.get("view")
            kernel = view.get("kernel") if isinstance(view, Mapping) else None
            if isinstance(kernel, Mapping) and kernel.get("kernel_id") in (None, kernel_id):
                env = kernel.get("env")
                found = env.get("env_id") if isinstance(env, Mapping) else None
        if isinstance(found, str) and 0 < len(found) <= MAX_ENV_ID:
            env_id = found
    return env_id


async def accept_events(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    sender_machine_id: str,
    holder_machine_id: str | None,
    kernel_id: str,
    state: str | None,
    events: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
) -> AcceptedBatch:
    """Accept one batch from ``sender_machine_id`` (the machine the request
    PROVED it is) for the notebook ``item_id`` of ``org_id``, and announce its
    new events on the notebook's channel. ``holder_machine_id`` is the machine
    holding the notebook's folder now, the only one a new kernel binds to.
    Raises :class:`KernelRefusedError`; nothing is written or announced then.
    The caller commits."""
    if state is not None and state not in KERNEL_STATES:
        raise KernelRefusedError("unknown kernel state")
    if len(events) > MAX_BATCH_EVENTS:
        raise KernelRefusedError("too many events in one batch")
    if seq_out_of_range(events) is not None:
        raise KernelRefusedError("an event's sequence is out of range")
    at = now or datetime.now(UTC)
    # One notebook's kernel batches are taken one at a time: a kernel that
    # comes up ends the others, and two doing so at once must not deadlock.
    await advisory_xact_lock(db, advisory_key("notebook-kernels", item_id))
    kernel = (
        await db.execute(
            select(NotebookKernel).where(NotebookKernel.kernel_id == kernel_id).with_for_update()
        )
    ).scalar_one_or_none()
    if kernel is None:
        if holder_machine_id is None or holder_machine_id != sender_machine_id:
            raise KernelRefusedError("only the folder's holder starts a kernel")
        # Inserted only when the id is free anywhere: an id another org's
        # kernel holds is not visible to this request (row security), and
        # must read exactly like one bound elsewhere.
        inserted = (
            await db.execute(
                pg_insert(NotebookKernel)
                .values(
                    kernel_id=kernel_id,
                    org_id=org_id,
                    drive_id=drive_id,
                    item_id=item_id,
                    machine_id=sender_machine_id,
                    state=state or "starting",
                    seq=0,
                    started_at=at,
                    updated_at=at,
                )
                .on_conflict_do_nothing(index_elements=["kernel_id"])
                .returning(NotebookKernel.kernel_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            raise KernelRefusedError("no such kernel")
        kernel = (
            await db.execute(
                select(NotebookKernel)
                .where(NotebookKernel.kernel_id == kernel_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    elif (
        kernel.org_id != org_id
        or kernel.machine_id != sender_machine_id
        or kernel.item_id != item_id
    ):
        # Opaque to the sender: a kernel of another org reads exactly like a
        # kernel that does not exist.
        raise KernelRefusedError("no such kernel")
    # Each event fitted to what one outbox row and one realtime frame carry
    # (an output too large to carry becomes a marker saying so), so no
    # kernel's output is ever a refused batch.
    fresh = [fit_event(event) for event in _clean_events(events, after=int(kernel.seq))]
    if state is None:
        state = state_of_events(fresh)
    if state is not None:
        kernel.state = state
        if state == "stopped":
            kernel.stopped_at = kernel.stopped_at or at
        elif state in LIVE_KERNEL_STATES:
            # The kernel that last said it runs is the notebook's kernel.
            kernel.stopped_at = None
    env_id = env_of_events(fresh, kernel_id)
    if env_id is not None:
        kernel.env_id = env_id
    if fresh:
        kernel.seq = int(fresh[-1]["seq"])
    kernel.updated_at = at
    if state in LIVE_KERNEL_STATES:
        # A restart's new kernel (or one on a box the folder moved to) ends
        # every other the notebook had, whether or not their exit was heard:
        # none of them runs any more.
        await db.execute(
            update(NotebookKernel)
            .where(
                NotebookKernel.org_id == org_id,
                NotebookKernel.item_id == item_id,
                NotebookKernel.kernel_id != kernel_id,
                NotebookKernel.stopped_at.is_(None),
            )
            .values(state="stopped", stopped_at=at, updated_at=at)
            .execution_options(synchronize_session=False)
        )
    await db.flush()
    if fresh or state is not None:
        # As many rows as the batch needs, each within the outbox's cap, in
        # order; the kernel's state rides each.
        for group in batches(fresh) or [[]]:
            await emit(
                db,
                org_id=org_id,
                type=EventType.NOTEBOOK_EVENT,
                entity=Entity.NOTEBOOK,
                entity_id=nb_channel(item_id),
                version=int(group[-1]["seq"]) if group else int(kernel.seq),
                payload={
                    "kernel_id": kernel_id,
                    "state": kernel.state,
                    "events": group,
                },
            )
    return AcceptedBatch(
        kernel_id=kernel_id, accepted=len(fresh), seq=int(kernel.seq), events=tuple(fresh)
    )


def platform_events_of(event: HubEvent) -> tuple[uuid.UUID, list[dict[str, Any]]] | None:
    """A hub event as ``(item_id, events)`` when it carries the platform's own
    events for a notebook channel; ``None`` otherwise."""
    if event.type != EventType.NOTEBOOK_EVENT.value or event.channel is None:
        return None
    item_id = item_of_channel(event.channel)
    raw = event.payload.get(PLATFORM_EVENTS_KEY)
    if item_id is None or not isinstance(raw, list):
        return None
    return item_id, [dict(one) for one in raw if isinstance(one, dict)]


async def current_kernel(
    db: AsyncSession, *, org_id: uuid.UUID, item_id: uuid.UUID
) -> NotebookKernel | None:
    """The notebook's most recently heard-of kernel that has not stopped. A
    channel that says ``absent`` (the box's events of no kernel) is none."""
    found: NotebookKernel | None = (
        await db.execute(
            select(NotebookKernel)
            .where(
                NotebookKernel.org_id == org_id,
                NotebookKernel.item_id == item_id,
                NotebookKernel.stopped_at.is_(None),
                NotebookKernel.state != "absent",
            )
            .order_by(NotebookKernel.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return found


# -- replay -----------------------------------------------------------------------


@dataclass(slots=True)
class _Ring:
    kernel_id: str | None = None
    snapshot: dict[str, Any] | None = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=RING_EVENTS))
    state: str | None = None
    offered: OrderedDict[str, None] = field(default_factory=OrderedDict)
    #: ``(module, version) -> the sha256 of the module's entry file``.
    modules: OrderedDict[tuple[str, str], str] = field(default_factory=OrderedDict)
    #: The last sequence heard of the kernel, whatever its event.
    seq: int = 0
    #: Whether ``events`` hold every event since the snapshot (or since the
    #: kernel began): false once the ring dropped one to make room or heard a
    #: later one than the next. Only a new snapshot makes it whole again.
    whole: bool = True
    #: When this replica last asked the box for a snapshot, while none came.
    asked_at: float | None = None


@dataclass(frozen=True, slots=True)
class Replay:
    """What a joining socket is sent: the snapshot (``None`` when this
    replica has none yet) and the events after it, of ``kernel_id``."""

    kernel_id: str | None
    seq: int
    state: str | None
    snapshot: dict[str, Any] | None
    events: list[dict[str, Any]]
    #: Whether ``events`` are every event after the snapshot. When they are
    #: not, a joiner is sent them and a snapshot is asked for.
    whole: bool = True


def batch_of(event: HubEvent) -> tuple[uuid.UUID, str, str | None, list[dict[str, Any]]] | None:
    """A hub event as ``(item_id, kernel_id, state, events)`` when it is a
    notebook batch on a notebook channel; ``None`` otherwise (a request to a
    machine travels as the same type on the machine's channel)."""
    if event.type != EventType.NOTEBOOK_EVENT.value or event.channel is None:
        return None
    item_id = item_of_channel(event.channel)
    kernel_id = event.payload.get("kernel_id")
    raw = event.payload.get("events")
    if item_id is None or not isinstance(kernel_id, str) or not isinstance(raw, list):
        return None
    state = event.payload.get("state")
    events = [dict(one) for one in raw if isinstance(one, dict)]
    return item_id, kernel_id, state if isinstance(state, str) else None, events


class NotebookFeed:
    """One per process, beside its hub (see the module docstring)."""

    def __init__(
        self, hub: EventHub | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._rings: OrderedDict[uuid.UUID, _Ring] = OrderedDict()
        self._hub = hub
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sub: Subscription | None = None

    def start(self) -> None:
        """Follow the hub's notebook batches.

        Taken in the hub's own publish, synchronously, never queued: an event
        is then either in a ring or still to be delivered to a socket that
        holds the channel, never between the two, so a socket that joins
        between a publish and its delivery cannot miss it."""
        if self._hub is None or self._sub is not None:
            return

        def take(event: HubEvent) -> bool:
            self.observe(event)
            return False

        self._sub = self._hub.subscribe(take, label="notebook-feed", maxsize=1)

    async def close(self) -> None:
        if self._hub is not None and self._sub is not None:
            self._hub.unsubscribe(self._sub)
        self._sub = None

    def _ring(self, item_id: uuid.UUID) -> _Ring:
        ring = self._rings.get(item_id)
        if ring is None:
            ring = self._rings[item_id] = _Ring()
            while len(self._rings) > RING_NOTEBOOKS:
                self._rings.popitem(last=False)
        self._rings.move_to_end(item_id)
        return ring

    def observe(self, event: HubEvent) -> None:
        """Take one hub event into the ring of its notebook."""
        found = batch_of(event)
        if found is None:
            return
        item_id, kernel_id, state, events = found
        ring = self._ring(item_id)
        if ring.kernel_id != kernel_id:
            ring.kernel_id = kernel_id
            ring.snapshot = None
            ring.events.clear()
            ring.offered.clear()
            ring.modules.clear()
            ring.seq, ring.whole, ring.asked_at = 0, True, None
        if state is not None:
            ring.state = state
        for one in events:
            _heard(ring, one.get("seq"))
            if one.get("type") == ANSWER_EVENT:
                self._answered(one)
                continue
            if one.get("type") == WIDGET_ASSET_EVENT:
                _take_module(ring, one)
            if one.get("type") == SNAPSHOT_EVENT:
                ring.snapshot = one
                ring.events.clear()
                ring.whole, ring.asked_at = True, None
                continue
            if one.get("type") == ASSET_OFFERED_EVENT:
                sha = one.get("sha256")
                if isinstance(sha, str):
                    ring.offered[sha] = None
                    while len(ring.offered) > OFFERED_ASSETS:
                        ring.offered.popitem(last=False)
            if len(ring.events) == ring.events.maxlen:
                ring.whole = False
            ring.events.append(one)

    def replay(self, item_id: uuid.UUID) -> Replay:
        """The snapshot and the events after it this replica holds."""
        ring = self._rings.get(item_id)
        if ring is None:
            return Replay(kernel_id=None, seq=0, state=None, snapshot=None, events=[])
        seq = int(ring.snapshot["seq"]) if ring.snapshot is not None else 0
        return Replay(
            kernel_id=ring.kernel_id,
            seq=seq,
            state=ring.state,
            snapshot=None if ring.snapshot is None else dict(ring.snapshot),
            events=[dict(one) for one in ring.events if int(one["seq"]) > seq],
            whole=ring.whole,
        )

    def note_hole(self, item_id: uuid.UUID) -> None:
        """A socket on the notebook was sent an event past the next one: what
        it holds is not whole until a snapshot comes."""
        self._ring(item_id).whole = False

    def wants_snapshot(self, item_id: uuid.UUID) -> bool:
        """Whether to ask the box for a snapshot of the notebook: this replica
        has none, or its events since the last one have a hole. True at most
        once per :data:`SNAPSHOT_ASK_SECONDS` while none arrives, so a crowd
        of joiners asks once."""
        ring = self._ring(item_id)
        if ring.snapshot is not None and ring.whole:
            return False
        now = self._clock()
        if ring.asked_at is not None and now - ring.asked_at < SNAPSHOT_ASK_SECONDS:
            return False
        ring.asked_at = now
        return True

    def expect(self, request_id: str) -> asyncio.Future[dict[str, Any]]:
        """A future the engine's answer to ``request_id`` resolves (with the
        answer event), on whichever replica the answer arrives: every replica
        hears every batch. Register it before the request leaves."""
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        return future

    def forget(self, request_id: str) -> None:
        future = self._waiting.pop(request_id, None)
        if future is not None and not future.done():
            future.cancel()

    def _answered(self, event: dict[str, Any]) -> None:
        future = self._waiting.pop(str(event.get("request_id")), None)
        if future is not None and not future.done():
            future.set_result(dict(event))

    def module(self, item_id: uuid.UUID, module: str, version: str) -> str | None:
        """The hash of ``module@version``'s entry file, when the notebook's
        current kernel offered it."""
        ring = self._rings.get(item_id)
        return None if ring is None else ring.modules.get((module, version))

    def offered(self, item_id: uuid.UUID, sha256: str) -> bool:
        """Whether the notebook's current kernel offered the asset ``sha256``."""
        ring = self._rings.get(item_id)
        return ring is not None and sha256 in ring.offered


def _heard(ring: _Ring, seq: Any) -> None:
    """Note a numbered event; one past the next is a hole in what this
    replica heard (a lost batch), and the ring is no longer whole."""
    if isinstance(seq, bool) or not isinstance(seq, int):
        return
    if seq > ring.seq + 1:
        ring.whole = False
    ring.seq = max(ring.seq, seq)


def _take_module(ring: _Ring, event: Mapping[str, Any]) -> None:
    """Remember a kernel's offer of a widget library's files: every file's
    hash may be served, and the module resolves to its entry file
    (``index.js``, else the first file offered)."""
    module, version, files = event.get("module"), event.get("version"), event.get("files")
    if not isinstance(module, str) or not isinstance(version, str) or not isinstance(files, list):
        return
    hashes: list[tuple[str, str]] = []
    for one in files:
        if not isinstance(one, Mapping):
            continue
        path, sha = one.get("path"), one.get("sha256")
        if isinstance(path, str) and isinstance(sha, str) and len(sha) == 64:
            hashes.append((path, sha.lower()))
    if not hashes:
        return
    for _, sha in hashes:
        ring.offered[sha] = None
    while len(ring.offered) > OFFERED_ASSETS:
        ring.offered.popitem(last=False)
    entry = next((sha for path, sha in hashes if path.rsplit("/", 1)[-1] == "index.js"), None)
    ring.modules[(module, version)] = entry or hashes[0][1]
    ring.modules.move_to_end((module, version))
    while len(ring.modules) > OFFERED_MODULES:
        ring.modules.popitem(last=False)


@dataclass(slots=True)
class ChannelCursor:
    """Where one socket stands on one notebook channel: the kernel and the
    last sequence it was sent. Every event is sent once, in order: one of
    another kernel moves it (a restart), one at or behind it is dropped. An
    event past the next one is a hole: ``gap`` says so until it is read."""

    kernel_id: str | None = None
    seq: int = 0
    gap: bool = False

    def admit(self, kernel_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if kernel_id != self.kernel_id:
            self.kernel_id, self.seq = kernel_id, 0
        fresh: list[dict[str, Any]] = []
        for one in events:
            seq = one.get("seq")
            if isinstance(seq, int) and seq > self.seq:
                if seq > self.seq + 1 and one.get("type") != SNAPSHOT_EVENT:
                    self.gap = True
                fresh.append(one)
                self.seq = seq
        return fresh

    def take_gap(self) -> bool:
        """Whether a hole was seen since the last call."""
        found, self.gap = self.gap, False
        return found

    def start_at(self, kernel_id: str | None, seq: int) -> None:
        self.kernel_id, self.seq = kernel_id, seq


FeedFactory = Callable[[], NotebookFeed]

__all__ = [
    "ANSWER_EVENT",
    "ASSET_OFFERED_EVENT",
    "KERNEL_STATES",
    "LIVE_KERNEL_STATES",
    "MAX_BATCH_EVENTS",
    "MAX_EVENT_SEQ",
    "RING_EVENTS",
    "SNAPSHOT_ASK_SECONDS",
    "SNAPSHOT_EVENT",
    "UNSENT_EVENTS",
    "WIDGET_ASSET_EVENT",
    "AcceptedBatch",
    "ChannelCursor",
    "KernelRefusedError",
    "NotebookFeed",
    "Replay",
    "accept_events",
    "batch_of",
    "current_kernel",
    "env_of_events",
    "platform_events_of",
    "seq_out_of_range",
    "state_of_events",
]
