"""The notebook channel on one person's socket (``nb:<item_id>``).

Held beside the socket's document channels and driven by the socket's own
loops: ``subscribe`` and ``drop`` from its inbound frames, ``outbound`` for
every hub event it accepted, ``recheck`` on its keepalive tick. Who may hold
the channel is the notebook document's own question (the same answer
``doc:notebook:<item_id>`` gets: Files READ), asked through ``decide``. A
widget message is decided for every message by ``deliver_comm`` exactly as
POST ``.../comm`` is: the ``notebook.run`` policy, on the record, then the
frame must be the sender's on this notebook.

Every frame is ``{t: "nb", channel, event}``. Joining sends a ``snapshot``
event (the notebook's view and the kernel's state at ``(kernel_id, seq)``),
then each buffered event after it once, then the live ones. An event
addressed to an output frame (``frame_id``) goes only to the sockets the
frame reaches (its owner's, narrowed to one when it was attached so).

Frames leave through one bounded queue drained by a sender task, so a socket
slower than its kernel is never sent a backlog: consecutive widget state
updates of one model are merged while they wait, and a queue that still fills
is replaced by one ``resync`` event for each channel it held events of.

The numbers a socket is sent rise but are not consecutive: a box's answers
(``UNSENT_EVENTS``) and events for frames the person does not hold are kept
back, and merged widget updates keep only the last number. A client orders
and deduplicates by number and never reads a loss from a skip; a ``resync``
is the only way this channel says events were lost.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from uuid import UUID

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.notebooks.channel import NB_FRAME_PREFIX, NbCommFrame, NbFrame
from alkera_core.notebooks.runs import NB_CHANNEL_PREFIX, item_of_channel
from alkera_core.schemas.realtime import ErrorFrame, RawFrame, SubscribedFrame
from pydantic import BaseModel, ValidationError

from backend.services.crdt import Access, CrdtError
from backend.services.notebooks import frames
from backend.services.notebooks.errors import CommRefusedError
from backend.services.notebooks.feed import (
    SNAPSHOT_EVENT,
    UNSENT_EVENTS,
    ChannelCursor,
    NotebookFeed,
    batch_of,
    platform_events_of,
)
from backend.services.notebooks.refs import doc_ref
from backend.services.realtime import EntitlementSnapshot
from backend.services.sharing import SharedRungCache

log = get_logger(__name__)

#: How many frames may wait for a slow socket before its channels resync.
OUTBOUND_FRAMES: Final = 256
#: The most notebook channels one socket holds.
MAX_NB_CHANNELS: Final = 32

Decide = Callable[[UUID], Awaitable[Access]]
DeliverComm = Callable[[UUID, NbCommFrame], Awaitable[None]]
AskSnapshot = Callable[[UUID], Awaitable[None]]
ViewOf = Callable[[UUID], Awaitable[dict[str, Any]]]
DetachFrames = Callable[[UUID], Coroutine[Any, Any, None]]
Send = Callable[[BaseModel], Awaitable[None]]


@dataclass(slots=True)
class _Held:
    item_id: UUID
    cursor: ChannelCursor = field(default_factory=ChannelCursor)
    #: Events heard while the join's snapshot was being read (``None`` once
    #: the snapshot is queued): sent after it, past its sequence. A kernel id
    #: of ``None`` marks the platform's own events, which have no sequence.
    waiting: list[tuple[str | None, list[dict[str, Any]]]] | None = None


def _widget_update(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """``(comm_id, state)`` for a widget state update (``comm.msg`` whose
    content says ``method: update`` and carries no binary buffers), which a
    later one of the same model may be merged into."""
    if event.get("type") != "comm.msg" or event.get("buffers"):
        return None
    comm_id = event.get("comm_id")
    content = event.get("content")
    if not isinstance(comm_id, str) or not isinstance(content, dict):
        return None
    data = content.get("data")
    if not isinstance(data, dict) or data.get("method") != "update" or data.get("buffer_paths"):
        return None
    state = data.get("state")
    return (comm_id, state) if isinstance(state, dict) else None


def coalesce(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``events`` with each run of consecutive state updates of one widget
    model merged into its last one (the later value of a key wins). Every
    other event, and the order of everything, is kept."""
    out: list[dict[str, Any]] = []
    for event in events:
        update = _widget_update(event)
        if update is not None and out:
            previous = _widget_update(out[-1])
            if (
                previous is not None
                and previous[0] == update[0]
                and out[-1].get("frame_id") == event.get("frame_id")
            ):
                merged_state = {**previous[1], **update[1]}
                merged = dict(event)
                content = dict(merged["content"])
                data = dict(content["data"])
                data["state"] = merged_state
                content["data"] = data
                merged["content"] = content
                out[-1] = merged
                continue
        out.append(event)
    return out


@dataclass
class NotebookSocket:
    """See the module docstring."""

    send: Send
    feed: NotebookFeed
    decide: Decide
    deliver_comm: DeliverComm
    view_of: ViewOf
    user_id: UUID
    peer_id: str | None = None
    ask_snapshot: AskSnapshot | None = None
    detach_frames: DetachFrames | None = None
    max_queued: int = OUTBOUND_FRAMES
    held: dict[str, _Held] = field(default_factory=dict)
    _queue: deque[NbFrame | BaseModel] = field(default_factory=deque, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _sender: asyncio.Task[None] | None = field(default=None, init=False)

    # -- subscribing ---------------------------------------------------------

    def holds(self, channel: str | None) -> bool:
        return channel is not None and channel in self.held

    async def subscribe(self, raw: str) -> None:
        """Hold ``raw`` when the person may read the notebook, then send the
        snapshot (with the notebook's view) and every buffered event after
        it, then what arrived while the view was read."""
        item_id = item_of_channel(raw)
        if item_id is None:
            await self.send(ErrorFrame(code="bad_channel", message="nb:<item id>", channel=raw))
            return
        if raw not in self.held and len(self.held) >= MAX_NB_CHANNELS:
            await self.send(ErrorFrame(code="too_many_channels", message="cap", channel=raw))
            return
        try:
            access = await self.decide(item_id)
        except CrdtError as exc:
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=raw))
            return
        self.start()
        # The replay is taken with no await before the channel is held: an
        # event published meanwhile is either in the feed's ring (taken at
        # publish) or delivered to this socket, which keeps it while the view
        # is read and sends it after the snapshot, past its sequence.
        held = self.held[raw] = _Held(item_id=item_id, waiting=[])
        replay = self.feed.replay(item_id)
        held.cursor.start_at(replay.kernel_id, replay.seq)
        self._enqueue(SubscribedFrame(channel=raw, can_write=access.can_write))
        try:
            view = await self.view_of(item_id)
        except CrdtError as exc:
            self.held.pop(raw, None)
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=raw))
            return
        if self.held.get(raw) is not held:
            return
        snapshot: dict[str, Any] = dict(replay.snapshot or {})
        snapshot.update(
            {
                "type": SNAPSHOT_EVENT,
                "kernel_id": replay.kernel_id,
                "seq": replay.seq,
                "view": view,
                "frames": {},
            }
        )
        if replay.state is not None:
            snapshot.setdefault("state", replay.state)
        self._enqueue(NbFrame(channel=raw, event=snapshot))
        waiting, held.waiting = held.waiting or [], None
        if replay.kernel_id is not None:
            self._admit(raw, held, replay.kernel_id, replay.events)
        for kernel_id, events in waiting:
            self._admit(raw, held, kernel_id, events)
        if self.feed.wants_snapshot(item_id):
            await self._ask(item_id)

    async def _ask(self, item_id: UUID) -> None:
        """Ask the box for a snapshot; best effort, since a later joiner or
        the next hole asks again."""
        if self.ask_snapshot is None:
            return
        with contextlib.suppress(Exception):
            await self.ask_snapshot(item_id)

    def drop(self, raw: str) -> bool:
        held = self.held.pop(raw, None)
        if held is None:
            return False
        self._detach(held.item_id)
        return True

    def drop_all(self) -> None:
        for held in list(self.held.values()):
            self._detach(held.item_id)
        self.held.clear()

    def _detach(self, item_id: UUID) -> None:
        """The socket left the notebook: its frames are detached at the hub."""
        if self.detach_frames is None:
            return
        task: asyncio.Task[None] = asyncio.get_event_loop().create_task(self.detach_frames(item_id))
        # Best effort: a hub that never hears it drops the frames with the
        # kernel; the outcome is only consumed so it is never logged as lost.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())

    async def recheck(self) -> list[str]:
        """Decide every held channel again; drop and return those the person
        may no longer read."""
        lost: list[str] = []
        for raw, held in list(self.held.items()):
            try:
                await self.decide(held.item_id)
            except CrdtError:
                self.held.pop(raw, None)
                self._detach(held.item_id)
                lost.append(raw)
        return lost

    # -- outbound ------------------------------------------------------------

    def wants(self, event: HubEvent) -> bool:
        return (
            batch_of(event) is not None or platform_events_of(event) is not None
        ) and self.holds(event.channel)

    def outbound(self, event: HubEvent) -> None:
        """Queue a held channel's events, past where this socket stands; the
        platform's own events, which no kernel numbered, as they come."""
        if event.channel is None:
            return
        held = self.held.get(event.channel)
        if held is None:
            return
        found = batch_of(event)
        kernel_id: str | None
        if found is not None:
            _, kernel_id, _state, events = found
        else:
            platform = platform_events_of(event)
            if platform is None:
                return
            kernel_id, events = None, platform[1]
        if held.waiting is not None:
            held.waiting.append((kernel_id, events))
            return
        self._admit(event.channel, held, kernel_id, events)

    def _admit(
        self, raw: str, held: _Held, kernel_id: str | None, events: list[dict[str, Any]]
    ) -> None:
        if kernel_id is None:
            for one in events:
                if self._for_me(one):
                    self._enqueue(NbFrame(channel=raw, event=one))
            return
        for one in held.cursor.admit(kernel_id, events):
            if self._for_me(one):
                self._enqueue(NbFrame(channel=raw, event={"kernel_id": kernel_id, **one}))
        if not held.cursor.take_gap():
            return
        # A batch this socket never heard: the box's snapshot brings the
        # notebook whole again.
        self.feed.note_hole(held.item_id)
        if self.feed.wants_snapshot(held.item_id):
            task = asyncio.get_event_loop().create_task(self._ask(held.item_id))
            task.add_done_callback(lambda done: done.cancelled() or done.exception())

    def _for_me(self, event: dict[str, Any]) -> bool:
        if event.get("type") in UNSENT_EVENTS:
            return False
        frame_id = event.get("frame_id")
        if frame_id is None:
            return True
        return isinstance(frame_id, str) and frames.reaches(
            frame_id, user_id=self.user_id, peer_id=self.peer_id
        )

    def _enqueue(self, frame: NbFrame | BaseModel) -> None:
        if isinstance(frame, NbFrame) and self._queue:
            last = self._queue[-1]
            if isinstance(last, NbFrame) and last.channel == frame.channel:
                merged = coalesce([last.event, frame.event])
                if len(merged) == 1:
                    self._queue[-1] = NbFrame(channel=frame.channel, event=merged[0])
                    self._wake.set()
                    return
        if len(self._queue) >= self.max_queued:
            self._overflow(frame)
            return
        self._queue.append(frame)
        self._wake.set()

    def _overflow(self, arriving: NbFrame | BaseModel) -> None:
        """Replace everything waiting, and the frame that did not fit, with
        one ``resync`` event per notebook channel they held events of; the
        client subscribes again."""
        channels: list[str] = []
        kept: deque[NbFrame | BaseModel] = deque()
        for frame in [*self._queue, arriving]:
            if isinstance(frame, NbFrame):
                if frame.channel not in channels:
                    channels.append(frame.channel)
            else:
                kept.append(frame)
        for channel in channels:
            held = self.held.pop(channel, None)
            if held is not None:
                self._detach(held.item_id)
            kept.append(NbFrame(channel=channel, event={"type": "resync"}))
        self._queue = kept
        self._wake.set()
        log.info("realtime.nb.resync", channels=len(channels))

    def start(self) -> None:
        if self._sender is None:
            self._sender = asyncio.create_task(self._drain(), name="ws-nb-outbound")

    async def close(self) -> None:
        self.drop_all()
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(BaseException):
                await self._sender
            self._sender = None

    async def _drain(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                await self.send(self._queue.popleft())

    async def flush(self) -> None:
        """Send whatever waits, now (for a socket without a sender task)."""
        while self._queue:
            await self.send(self._queue.popleft())

    # -- inbound -------------------------------------------------------------

    async def inbound(self, text: str) -> None:
        """One ``nb.*`` frame from the client."""
        try:
            frame = NbCommFrame.model_validate_json(text)
        except ValidationError:
            await self.send(ErrorFrame(code="bad_frame", message="unknown or malformed nb frame"))
            return
        held = self.held.get(frame.channel)
        if held is None:
            await self.send(
                ErrorFrame(code="not_subscribed", message="subscribe first", channel=frame.channel)
            )
            return
        try:
            await self.deliver_comm(held.item_id, frame)
        except CommRefusedError as exc:
            if exc.lost and self.held.pop(frame.channel, None) is not None:
                self._detach(held.item_id)
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=frame.channel))


class PersonSocket(Protocol):
    """What :func:`notebook_socket_for` reads of a person's socket."""

    runtime: Any
    user: User
    agent_id: str | None
    verified_machine_id: str | None
    rungs: SharedRungCache
    peer_id: str

    @property
    def org_id(self) -> UUID: ...

    @property
    def ent(self) -> EntitlementSnapshot: ...

    async def send(self, frame: Any) -> None: ...


def notebook_socket_for(host: PersonSocket) -> NotebookSocket | None:
    """The notebook channels of a person's socket, decided by the CRDT lane's
    ``notebook`` type (the very answer ``doc:notebook:<item_id>`` gets) and
    served by the runtime's notebook service; ``None`` when the process runs
    neither."""
    runtime = host.runtime
    docs = getattr(runtime, "crdt", None)
    service = getattr(runtime, "notebooks", None)
    if docs is None or service is None:
        return None

    async def decide(item_id: UUID) -> Access:
        async with AsyncSessionLocal() as db:
            try:
                access: Access = await docs.access(
                    db,
                    doc_ref(host.org_id, item_id),
                    user=host.user,
                    ent=host.ent,
                    agent_id=host.agent_id,
                    machine_id=host.verified_machine_id,
                    rungs=host.rungs,
                )
            finally:
                await db.rollback()
        return access

    async def deliver(item_id: UUID, frame: NbCommFrame) -> None:
        await service.comm_from_socket(
            org_id=host.org_id,
            item_id=item_id,
            user=host.user,
            agent_id=host.agent_id,
            frame=frame,
        )

    async def ask(item_id: UUID) -> None:
        await service.ask_snapshot(org_id=host.org_id, item_id=item_id, user=host.user)

    async def view_of(item_id: UUID) -> dict[str, Any]:
        view = await service.view_for(org_id=host.org_id, item_id=item_id)
        dumped: dict[str, Any] = view.model_dump(mode="json")
        return dumped

    async def detach(item_id: UUID) -> None:
        await service.detach_frames(
            org_id=host.org_id, item_id=item_id, user=host.user, peer_id=host.peer_id
        )

    return NotebookSocket(
        send=host.send,
        feed=service.feed,
        decide=decide,
        deliver_comm=deliver,
        view_of=view_of,
        user_id=host.user.id,
        peer_id=host.peer_id,
        ask_snapshot=ask,
        detach_frames=detach,
    )


# -- what a realtime socket asks of its notebook channels ------------------------
# The socket session holds a NotebookSocket only for a person in a process that
# serves notebooks; these take ``None`` for every other socket, so the session
# routes its frames and events here with one call each.


async def nb_subscribe(sock: NotebookSocket | None, send: Send, raw: str) -> bool:
    """Whether ``raw`` names a notebook channel (handled here)."""
    if not raw.startswith(NB_CHANNEL_PREFIX):
        return False
    if sock is None:
        await send(ErrorFrame(code="not_found", message="notebooks are not served", channel=raw))
    else:
        await sock.subscribe(raw)
    return True


def nb_unsubscribe(sock: NotebookSocket | None, raw: str) -> bool:
    """Whether ``raw`` was a notebook channel this socket held (dropped)."""
    return sock is not None and sock.drop(raw)


async def nb_inbound(sock: NotebookSocket | None, frame: Any, text: str) -> bool:
    """Whether ``frame`` is an ``nb.*`` frame for this socket (handled)."""
    if sock is None or not isinstance(frame, RawFrame) or not frame.t.startswith(NB_FRAME_PREFIX):
        return False
    await sock.inbound(text)
    return True


def nb_outbound(sock: NotebookSocket | None, event: HubEvent) -> bool:
    """Whether ``event`` is a held notebook channel's (queued here)."""
    if sock is None or not sock.wants(event):
        return False
    sock.outbound(event)
    return True


def nb_holds(sock: NotebookSocket | None, channel: str | None) -> bool:
    return sock is not None and sock.holds(channel)


async def nb_reconsider(sock: NotebookSocket | None) -> None:
    """Decide every held notebook channel again; one the holder may no
    longer read is dropped and answered ``not_found``."""
    if sock is None or not sock.held:
        return
    for raw in await sock.recheck():
        await sock.send(ErrorFrame(code="not_found", channel=raw))


async def nb_close(sock: NotebookSocket | None) -> None:
    if sock is not None:
        await sock.close()


__all__ = [
    "MAX_NB_CHANNELS",
    "OUTBOUND_FRAMES",
    "NotebookSocket",
    "PersonSocket",
    "coalesce",
    "nb_close",
    "nb_holds",
    "nb_inbound",
    "nb_outbound",
    "nb_reconsider",
    "nb_subscribe",
    "nb_unsubscribe",
    "notebook_socket_for",
]
