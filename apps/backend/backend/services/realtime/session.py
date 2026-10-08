"""One realtime socket: the handshake, then three loops until it ends.

The handshake decides everything before the first frame: the browser
``Origin`` (a page outside the allowed set is refused), the ticket (offered
in the ``Sec-WebSocket-Protocol`` header as ``alkera-ticket.<jwt>`` — the
only place a credential is read from; there is no cookie fallback and no
query string), its single use (burned in its own committed transaction, so a
replay on any replica is refused), the session it was minted from (re-checked
for revocation without sliding the idle window), and capacity. Every refusal
closes the accepted socket with a code in the 44xx range the client can act
on; nothing is sent before the close.

Then, until the socket ends:

* the outbound loop drains the hub subscription — durable ``doc.op`` rows and
  ephemeral chunks and presence for the subscribed channels, filtered per
  frame by the same rule as the event stream — and writes frames;
* the inbound loop reads client frames (size-capped, rate-limited, parsed
  with an unknown tag answered in-band) and runs each handler in a short
  database session committed before the reply;
* the tick, every keepalive interval, re-checks the session (revocation,
  deactivation, the verification gate, the session's own expiry, the socket's
  maximum life), refreshes the entitlement snapshot and heartbeats presence.

On the way out: presence is left, the subscription dropped, the capacity slot
freed, exactly once. A shutting-down process closes its sockets itself (the
transport sends ``1012``, service restart) and, for one still running at the
graceful deadline, cancels the connection; the cancel is read as a
``SERVER_RESET`` close rather than left to orphan the three loops.

Two kinds of socket share the loops and differ in who holds them. A PERSON's
socket (:class:`SocketSession`) is admitted on a ticket minted from their
session and reads what their memberships and shares admit; it may speak as an
agent — its operator's box — when the ticket's assertion verifies as a machine
that person registered. A MACHINE's socket (:class:`MachineSocketSession`) is
admitted on a ticket minted from a box's own machine credential: nobody is
behind it, it holds exactly the chats bound to its machine and its own machine
channel, in whichever orgs its credential serves, and its tick re-asks the
credential's standing so a box the platform took away is cut within one tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.auth import (
    InvalidTokenError,
    SessionClaims,
    TokenRevokedError,
    WsMachineTicket,
    assert_token_active,
    decode_socket_ticket,
    family_alive,
    slide_family_idle,
)
from alkera_core.auth.tenancy import claims_stand, membership_stands
from alkera_core.authz import ActingContext
from alkera_core.compute.machines import verify_machine_assertion
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import (
    MAX_FRAME_BYTES,
    EventType,
    HubEvent,
    ResetMarker,
    actor_for_user,
)
from alkera_core.files.promotion import machine_event
from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.observability.context import bind_log_context, bind_trace_id, new_trace_id
from alkera_core.schemas.chat import NON_PERSISTED_EVENT_TYPES
from alkera_core.schemas.realtime import (
    CRDT_DOC_TYPES,
    MACHINE_FRAME_PREFIX,
    MACHINE_REQUEST_TAG,
    REALTIME_CLIENT_GENERATION,
    SERVER_PEER_ID,
    WS_SUBPROTOCOL,
    AckPayload,
    DocEnvelope,
    DocFrame,
    ErrorFrame,
    ErrorPayload,
    MachineRequest,
    PingFrame,
    PongFrame,
    PresenceCursor,
    PresenceCursorFrame,
    PresenceFrame,
    PresenceHeartbeatFrame,
    PresenceJoinFrame,
    PresenceLeaveFrame,
    PresencePeer,
    RawFrame,
    ReloadPayload,
    ResetFrame,
    SnapshotPayload,
    SocketLimits,
    SubscribedFrame,
    SubscribeFrame,
    UnsubscribeFrame,
    WelcomeFrame,
    dump_frame,
    parse_client_frame,
    parse_machine_frame,
)
from alkera_core.schemas.realtime import machine as machine_wire
from alkera_core.verification import is_blocked
from fastapi import WebSocket
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect, WebSocketState

from backend.services.crdt.gateway import CrdtSocket
from backend.services.notebooks import (
    NotebookSocket,
    nb_close,
    nb_holds,
    nb_inbound,
    nb_outbound,
    nb_reconsider,
    nb_subscribe,
    nb_unsubscribe,
    notebook_socket_for,
)
from backend.services.realtime import (
    box_channels,
    close_codes,
    presence,
    publisher,
    spelling,
    tickets,
)
from backend.services.realtime.channels import (
    Channel,
    ChannelError,
    ChannelGrant,
    MachineChannel,
    PresenceGrant,
    WorkspaceChannel,
    authorize,
    authorize_machine,
    authorize_workspace,
    parse_channel,
)
from backend.services.realtime.docsync import (
    REBUILD_REASON_PUBLISHER,
    DocOpRejectedError,
    DocRegistry,
    MachineSubject,
    StaleEpochError,
    Subject,
)
from backend.services.realtime.filters import (
    EntitlementRef,
    EntitlementSnapshot,
    MachineScopeRef,
    load_entitlements,
    load_machine_scope,
    machine_reauthorizes,
    machine_visible_to,
    reauthorizes,
    visible_to,
)
from backend.services.realtime.handshake import (
    Admission,
    MachineAdmission,
    Refusal,
    allowed_origins,
    origin_allowed,
    session_claims_for,
    ticket_from_subprotocols,
)
from backend.services.realtime.rates import FRAME_WINDOW_SECONDS as FRAME_WINDOW_SECONDS
from backend.services.realtime.rates import MAX_FRAMES_PER_WINDOW as MAX_FRAMES_PER_WINDOW
from backend.services.realtime.rates import ByteRate as _ByteRate
from backend.services.realtime.rates import FrameRate as _FrameRate
from backend.services.realtime.regrant import reconsider_documents, redecide
from backend.services.realtime.runtime import INSTANCE_ID, RealtimeRuntime
from backend.services.realtime.tick import BeatFaults, session_deadline_seconds
from backend.services.sharing import SharedRungCache

log = get_logger(__name__)

#: The box coalesces a chat's stream into one frame every 50 ms and appends one
#: durable op per harness event on top of that, so a fast-streaming turn alone
#: fills the reader window — and closing that socket costs the turn the chunks
#: in the reconnect gap. A publisher therefore gets its own window, far above the
#: cadence it publishes at, so the bound still catches a socket looping on
#: nothing without ever firing on a busy turn; what actually bounds the work this
#: process does is the publisher BYTE budget beside it. Readers keep theirs: a
#: person's socket sends a subscribe, a presence beat and a cursor.
PUBLISHER_CHUNK_INTERVAL_SECONDS = 0.05
#: How many channels a PERSON's socket may hold: a browser tab watches a few
#: chats and a folder or two, and a socket asking for more is looping or
#: probing. It is not a bound on a workspace machine: the machine publishes for
#: every chat it serves and holds one channel per chat, hundreds on a busy box,
#: and what bounds its work is the publisher byte budget and its own capacity.
MAX_CHANNELS = 32
#: How many forwarded machine requests a box's socket remembers the org of, so
#: its ack is published where the drive that asked is listening. A request is
#: answered within its own deadline; anything older than the newest few
#: thousand is a request nobody is waiting on.
FORWARDED_REQUEST_MEMORY = 4096


async def admit(
    websocket: WebSocket, *, now: float | None = None
) -> Admission | MachineAdmission | Refusal:
    """Decide the handshake. Pure decision, no frames sent: the caller
    accepts the socket and either runs it or closes it with the refusal."""
    if not origin_allowed(websocket.headers.get("origin")):
        return Refusal(close_codes.ORIGIN_FORBIDDEN, "origin not allowed")
    offered = list(websocket.scope.get("subprotocols") or [])
    if WS_SUBPROTOCOL not in offered:
        return Refusal(close_codes.UNAUTHORIZED, f"subprotocol {WS_SUBPROTOCOL} required")
    raw = ticket_from_subprotocols(offered)
    if raw is None:
        return Refusal(close_codes.UNAUTHORIZED, "ticket required")
    try:
        ticket = decode_socket_ticket(raw)
    except InvalidTokenError:
        return Refusal(close_codes.UNAUTHORIZED, "invalid ticket")
    if not await tickets.burn(ticket.jti):
        return Refusal(close_codes.UNAUTHORIZED, "ticket already used")
    if isinstance(ticket, WsMachineTicket):
        return await _admit_machine(ticket)
    moment = time.time() if now is None else now
    if moment >= ticket.session_expires_at:
        return Refusal(close_codes.UNAUTHORIZED, "session expired")
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.id == ticket.user_id))
        ).scalar_one_or_none()
        if user is None or not user.is_active or is_blocked(user):
            return Refusal(close_codes.UNAUTHORIZED, "session ended")
        claims = session_claims_for(user, ticket)
        if not await claims_stand(db, claims, user=user):
            return Refusal(close_codes.UNAUTHORIZED, "membership in the organization ended")
        try:
            await assert_token_active(db, claims, user)
        except TokenRevokedError:
            return Refusal(close_codes.UNAUTHORIZED, "session revoked")
        await slide_family_idle(db, claims)
        await db.commit()
    return Admission(user=user, claims=claims, ticket=ticket)


async def _machine_context_for(
    db: AsyncSession, *, credential_id: UUID, machine_id: UUID, org_id: UUID, org_bound: bool
) -> ActingContext | None:
    """The context a machine ticket resolves to NOW, or ``None`` when its credential no
    longer stands behind its machine: the rule every door shares, asked on every tick."""
    from backend.auth.dependencies import machine_context_if_standing

    return await machine_context_if_standing(
        db, credential_id=credential_id, machine_id=machine_id, org_id=org_id, org_bound=org_bound
    )


async def _admit_machine(ticket: WsMachineTicket) -> MachineAdmission | Refusal:
    """The machine half of the handshake: the ticket's credential must still stand behind
    its machine. One answer for a revoked, rotated-away or unclaimed one and a gone machine."""
    async with AsyncSessionLocal() as db:
        ctx = await _machine_context_for(
            db,
            credential_id=ticket.credential_id,
            machine_id=ticket.machine_id,
            org_id=ticket.org_id,
            org_bound=ticket.org_bound,
        )
    if ctx is None:
        return Refusal(close_codes.UNAUTHORIZED, "machine credential refused")
    return MachineAdmission(ticket=ticket, ctx=ctx)


async def refuse(websocket: WebSocket, refusal: Refusal) -> None:
    """Accept (so the close code is observable) and close with the code."""
    offered = list(websocket.scope.get("subprotocols") or [])
    subprotocol = WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None
    with contextlib.suppress(Exception):
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=refusal.code, reason=refusal.reason)


def mint_peer_id() -> str:
    return f"p:{secrets.token_hex(6)}"


# ---------------------------------------------------------------------------
# The socket
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class _SocketCore:
    """The loops, the frame parsing and the document protocol every socket
    runs. Who holds the socket — a person, or a box on its credential — is the
    subclass's, and so is everything that follows from it: which frames it
    may hear, which channels it may hold, what it is re-checked against on
    the tick, and whether it has a presence at all."""

    websocket: WebSocket
    peer_id: str
    runtime: RealtimeRuntime
    registry: DocRegistry
    channels: dict[str, PresenceGrant] = field(default_factory=dict)
    #: The ``machine:<id>`` channels this socket holds — at most its own, and
    #: only when ``machine_id`` is set. Kept apart from ``channels``: a machine
    #: channel has no document, no presence and no write question to settle.
    machine_channels: set[str] = field(default_factory=set)
    #: The node rungs this connection has already read, so a re-subscribe does
    #: not pay for the ACL again. Only what the client is TOLD comes from here;
    #: every write is decided by the registry's own read under the row lock.
    rungs: SharedRungCache = field(default_factory=SharedRungCache)
    joined: set[str] = field(default_factory=set)
    respell: spelling.Respeller = field(default_factory=spelling.Respeller)
    closed: bool = False
    #: Whether the person did something on this socket since the last tick —
    #: an edit or a cursor move, never a ping, a presence heartbeat or a
    #: (re)subscribe a client does on its own. Only that moves an idle window.
    _acted: bool = field(default=False, init=False)
    #: The Loro CRDT lane on this socket, for a holder who may use it (a
    #: person; a box never does). ``None`` when the process runs no lane.
    crdt: CrdtSocket | None = field(default=None, init=False)
    #: The notebook channels (``nb:<item_id>``), for a person; ``None`` otherwise.
    notebooks: NotebookSocket | None = field(default=None, init=False)
    _rate: _FrameRate = field(default_factory=_FrameRate, init=False)
    _bytes: _ByteRate = field(
        default_factory=lambda: _ByteRate(budget=settings.realtime_ws_max_bytes_per_window),
        init=False,
    )

    def limits(self) -> SocketLimits:
        """The inbound budget this socket is held to, as the client is told it."""
        return SocketLimits(
            frames_per_window=self._rate.limit,
            bytes_per_window=self._bytes.budget,
            window_seconds=self._rate.window,
            max_frame_bytes=MAX_FRAME_BYTES,
            ephemeral_max_bytes=settings.realtime_ephemeral_max_bytes,
            doc_max_bytes=settings.realtime_doc_max_bytes,
            presence_ttl_seconds=settings.realtime_presence_ttl_seconds,
        )

    def _publisher_windows(self) -> None:
        """Hold this socket to the publisher's frame and byte windows."""
        self._rate = _FrameRate(limit=settings.realtime_ws_publisher_max_frames_per_window)
        self._bytes = _ByteRate(budget=settings.realtime_ws_publisher_max_bytes_per_window)

    # -- what the subclass says about its holder ---------------------------

    @property
    def subject(self) -> Subject:
        """Who the registry judges this socket's operations for."""
        raise NotImplementedError

    @property
    def actor(self) -> Mapping[str, Any]:
        """The actor document on every row this socket writes."""
        raise NotImplementedError

    @property
    def ent(self) -> EntitlementSnapshot:
        """What the holder is entitled to, as of the last tick."""
        raise NotImplementedError

    @property
    def machine_id(self) -> str | None:
        """The workspace machine this socket was VERIFIED to speak as, or
        ``None``. This, never a bare assertion, is what the channel rules and
        the registry compare with a chat's binding."""
        raise NotImplementedError

    @property
    def label(self) -> str:
        """The hub subscription's label."""
        raise NotImplementedError

    def _accept_event(self, event: HubEvent) -> bool:
        raise NotImplementedError

    def _reauthorizes(self, event: HubEvent) -> bool:
        """Whether ``event`` can have changed what this socket may hold."""
        raise NotImplementedError

    async def _reconsider_writing(self) -> None:
        """Decide the held channels again after a reauthorizing event."""
        raise NotImplementedError

    async def _reconsider_documents(self, only: str | None = None) -> None:
        """Decide the held live documents with a source again (all, or ``only``)."""

    async def _grant_for(
        self, channel: Channel | WorkspaceChannel
    ) -> tuple[PresenceGrant, list[PresencePeer]]:
        """The holder's grant on a channel and the roster it is told, or
        ``ChannelError``."""
        raise NotImplementedError

    async def _presence(self, raw: str, event: str) -> None:
        raise NotImplementedError

    async def _cursor(self, raw: str, cursor: PresenceCursor) -> None:
        raise NotImplementedError

    async def _leave(self, grant: PresenceGrant) -> None:
        raise NotImplementedError

    async def _machine_frame(self, text: str) -> None:
        raise NotImplementedError

    async def _recheck(self) -> bool:
        """Whether the holder's standing survives the tick."""
        raise NotImplementedError

    async def _still_the_machine(self) -> bool:
        """Re-ask, on the tick, whether a socket admitted as a machine is
        still one. Asked only when ``machine_id`` is set."""
        raise NotImplementedError

    async def _heartbeat_joined(self) -> None:
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        # Held by the runtime for as long as the loops run, so a draining
        # process closes this socket before its hub stops feeding it.
        self.runtime.hold(self)
        try:
            if self.runtime.draining:
                # Admitted while the drain was already closing the others.
                await self.close(close_codes.SERVICE_RESTART, "server restarting")
                return
            await self._run_loops()
        finally:
            self.runtime.release(self)

    async def _run_loops(self) -> None:
        sub = self.runtime.hub.subscribe(self._accept_event, label=self.label)
        outbound = asyncio.create_task(self._outbound(sub), name="ws-outbound")
        inbound = asyncio.create_task(self._inbound(), name="ws-inbound")
        tick = asyncio.create_task(self._tick(), name="ws-tick")
        tasks = {outbound, inbound, tick}
        try:
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                # The process is going away and this connection was cancelled at
                # its graceful deadline. Say so in the vocabulary the client
                # already reconnects on, and do not re-raise: the three loops
                # are separate tasks that a cancel escaping here would orphan,
                # still holding the socket and the subscription.
                done, pending = set(), tasks
                await self.close(close_codes.SERVER_RESET, "server closing")
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(BaseException):
                    await task
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    log.warning(
                        "realtime.ws.loop_failed",
                        loop=task.get_name(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await self.close(close_codes.SERVER_RESET, "internal error")
        finally:
            self.runtime.hub.unsubscribe(sub)
            await self._leave_everything()
            if self.crdt is not None:
                await self.crdt.drop_all()
            await nb_close(self.notebooks)
            if self.websocket.application_state == WebSocketState.CONNECTED and not self.closed:
                await self.close(close_codes.SERVER_RESET, "server closing")

    async def close(self, code: int, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason)

    async def send(self, frame: Any) -> None:
        if self.closed:
            return
        try:
            await self.websocket.send_text(dump_frame(self.respell.outbound(frame)))
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True

    # -- outbound ----------------------------------------------------------

    async def _outbound(self, sub: Any) -> None:
        while not self.closed:
            item = await sub.queue.get()
            if isinstance(item, ResetMarker):
                self.runtime.hub.ack_reset(sub)
                await self.send(ResetFrame(reason="overflow"))
                continue
            if self._reauthorizes(item):
                if self.crdt is not None:
                    self.crdt.observe(item)
                await self._reconsider_writing()
                await nb_reconsider(self.notebooks)
                continue
            if nb_outbound(self.notebooks, item):
                continue
            doc_frames = await self._doc_frames(item)
            if doc_frames is not None:
                for doc_frame in doc_frames:
                    await self.send(doc_frame)
                continue
            frame = self._frame_for(item)
            if frame is not None:
                await self.send(frame)

    async def _doc_frames(self, event: HubEvent) -> list[Any] | None:
        """The frames a live document's event becomes on this socket's CRDT
        lane; ``None`` for an event that is not one."""
        if self.crdt is None or not self.crdt.wants(event):
            return None
        saving = self.crdt.saving_notice(event)
        if saving is not None:
            await self._reconsider_documents(only=saving)
        return await self.crdt.outbound(event)

    def _frame_for(self, event: HubEvent) -> Any | None:
        if event.type == MACHINE_REQUEST_TAG or (
            # A notebook request rides the outbox to the machine's channel.
            event.type == EventType.NOTEBOOK_EVENT.value and event.channel in self.machine_channels
        ):
            return self._machine_request(event)
        if event.type in (EventType.DOC_OP.value, presence.CHUNK_EVENT_TYPE):
            raw = event.payload.get("envelope")
            if not isinstance(raw, dict):
                return None
            try:
                envelope = DocEnvelope.model_validate(raw)
            except ValidationError:
                log.warning("realtime.ws.bad_envelope", channel=event.channel)
                return None
            # The sender was answered with an ack; echoing its own op back
            # would make it apply the change twice.
            if envelope.kind == "op" and envelope.peer_id == self.peer_id:
                return None
            return DocFrame(envelope=envelope)
        if event.type == presence.PUBLISHER_EVENT_TYPE:
            return publisher.publisher_frame(event, machine_id=self.machine_id)
        if event.type == presence.PRESENCE_EVENT_TYPE:
            return presence.presence_frame(event)
        return None

    def _machine_request(self, event: HubEvent) -> machine_wire.AnyMachineRequest | None:
        """A request for the machine this socket proved it is, as the frame it
        is. Only a socket holding ``machine:<its own id>`` has the channel in
        its set; its own acks travel on it and are never echoed back."""
        if event.channel not in self.machine_channels:
            return None
        try:
            return machine_wire.parse_machine_request(event.payload)
        except ValidationError:
            log.warning("realtime.ws.bad_machine_request", channel=event.channel)
            return None

    # -- inbound -----------------------------------------------------------

    async def _inbound(self) -> None:
        async for text in self.websocket.iter_text():
            if self.closed:
                return
            size = len(text.encode("utf-8"))
            if size > MAX_FRAME_BYTES:
                await self.send(ErrorFrame(code="frame_too_large", message="frames are capped"))
                await self.close(close_codes.FRAME_TOO_LARGE, "frame too large")
                return
            now = time.monotonic()
            if not self._rate.allow(now):
                await self.close(close_codes.TOO_MANY, "too many frames")
                return
            if not self._bytes.allow(now, size):
                await self.close(close_codes.TOO_MANY, "too many bytes")
                return
            try:
                frame = self.respell.inbound(parse_client_frame(text))
            except ValidationError as exc:
                await self.send(
                    ErrorFrame(code="bad_frame", message=str(exc.errors()[0].get("msg", "")))
                )
                continue
            if isinstance(frame, RawFrame) and frame.t.startswith(MACHINE_FRAME_PREFIX):
                await self._machine_frame(text)
                continue
            if await nb_inbound(self.notebooks, frame, text):
                self._acted = True
                continue
            await self._dispatch(frame)

    async def _dispatch(self, frame: Any) -> None:
        if isinstance(frame, PingFrame):
            await self.send(PongFrame())
        elif isinstance(frame, SubscribeFrame):
            await self._subscribe(frame.channel)
        elif isinstance(frame, UnsubscribeFrame):
            await self._unsubscribe(frame.channel)
        elif isinstance(frame, PresenceJoinFrame):
            await self._presence(frame.channel, "join")
        elif isinstance(frame, PresenceHeartbeatFrame):
            await self._presence(frame.channel, "heartbeat")
        elif isinstance(frame, PresenceLeaveFrame):
            await self._presence(frame.channel, "leave")
        elif isinstance(frame, PresenceCursorFrame):
            self._acted = True
            await self._cursor(frame.channel, frame.cursor)
        elif isinstance(frame, DocFrame):
            if frame.envelope.kind in ("op", "crdt"):
                self._acted = True
            await self._doc(frame.envelope)
        elif isinstance(frame, RawFrame):
            await self.send(ErrorFrame(code="unknown_frame", message=f"unknown frame {frame.t!r}"))

    async def _subscribe(self, raw: str) -> None:
        if await nb_subscribe(self.notebooks, self.send, raw):
            return
        try:
            channel = parse_channel(raw)
        except ChannelError as exc:
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=raw))
            return
        if isinstance(channel, MachineChannel):
            await self._subscribe_machine(channel)
            return
        held = len(self.channels) + len(self.machine_channels)
        # The count is a person's bound. The socket that proved it is a machine
        # holds a channel for every chat it serves, and a cap here would leave
        # the chat after the cap unpublished on a box with idle slots to spare.
        if channel.key not in self.channels and self.machine_id is None and held >= MAX_CHANNELS:
            await self.send(
                ErrorFrame(code="too_many_channels", message="channel cap", channel=channel.key)
            )
            return
        try:
            grant, peers = await self._grant_for(channel)
        except ChannelError as exc:
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=channel.key))
            return
        self.channels[channel.key] = grant
        await self.send(SubscribedFrame(channel=channel.key, can_write=grant.can_write))
        await self.send(PresenceFrame(channel=channel.key, event="roster", peers=peers))
        await publisher.subscribed(self.machine_id, grant)

    async def _subscribe_machine(self, channel: MachineChannel) -> None:
        """Hold ``machine:<id>`` for the socket that proved it is that machine.

        Answered with ``subscribed`` alone: the channel has no roster, and
        ``can_write`` says the box may answer on it."""
        try:
            authorize_machine(channel, machine_id=self.machine_id)
        except ChannelError as exc:
            await self.send(ErrorFrame(code=exc.code, message=exc.message, channel=channel.key))
            return
        self.machine_channels.add(channel.key)
        await self.send(SubscribedFrame(channel=channel.key, can_write=True))

    async def _unsubscribe(self, raw: str) -> None:
        if nb_unsubscribe(self.notebooks, raw):
            return
        self.machine_channels.discard(raw)
        grant = self.channels.pop(raw, None)
        if grant is None:
            return
        if self.crdt is not None:
            await self.crdt.drop(raw)
        if raw in self.joined:
            await self._leave(grant)

    async def _leave_everything(self) -> None:
        for key in list(self.joined):
            grant = self.channels.get(key)
            if grant is not None:
                await self._leave(grant)
        await publisher.socket_ended(self.machine_id, self.runtime.draining, self.channels.values())

    # -- documents ---------------------------------------------------------

    def _doc_error(
        self, envelope: DocEnvelope, code: str, message: str = "", *, epoch: int | None = None
    ) -> DocFrame:
        # A refused op is answered BY ID, so the sender settles that op and
        # only that op; a refused hello or snapshot names none.
        op_id = envelope.payload.get("op_id") if envelope.kind == "op" else None
        named = str(op_id)[:64] if isinstance(op_id, str) and op_id else None
        return DocFrame(
            envelope=DocEnvelope(
                doc_id=envelope.doc_id,
                doc_type=envelope.doc_type,
                epoch=epoch if epoch is not None else max(envelope.epoch, 1),
                peer_id=SERVER_PEER_ID,
                seq=0,
                kind="error",
                payload=ErrorPayload(code=code, message=message, op_id=named).model_dump(
                    mode="json"
                ),
            )
        )

    async def _doc(self, envelope: DocEnvelope) -> None:
        # The peer id on an inbound envelope is not trusted: it is replaced
        # with this socket's server-minted id before anything reads it — the
        # last-writer-wins tie-break, the rebroadcast every other subscriber
        # sees, the event-log record and the echo suppression that keeps a
        # sender's op from coming back to it. Overwriting rather than
        # refusing keeps a client with a stale or mistaken id working; a
        # client naming another peer can neither speak as it nor silence it.
        if envelope.peer_id != self.peer_id:
            envelope = envelope.model_copy(update={"peer_id": self.peer_id})
        grant = self.channels.get(envelope.channel)
        if not isinstance(grant, ChannelGrant):
            await self.send(self._doc_error(envelope, "not_subscribed", "subscribe first"))
            return
        if grant.channel.doc_type in CRDT_DOC_TYPES:
            if self.crdt is None:
                await self.send(
                    self._doc_error(envelope, "read_only", "this socket only follows the document")
                )
                return
            await self.crdt.handle(grant, envelope)
            return
        if envelope.kind == "hello":
            await self._hello(grant, envelope)
        elif envelope.kind == "op":
            if envelope.payload.get("intent") == "chunk":
                await self._chunk(grant, envelope)
            else:
                await self._op(grant, envelope)
        elif envelope.kind == "snapshot":
            await self._publisher_snapshot(grant, envelope)
        else:
            await self.send(
                self._doc_error(
                    envelope, "unsupported_kind", f"a client may not send {envelope.kind!r}"
                )
            )

    async def _settle(
        self,
        grant: ChannelGrant,
        *,
        owner_user_id: UUID | None,
        team_id: UUID | None,
        can_write: bool,
    ) -> ChannelGrant:
        """Re-derive the cached grant from the row the registry just judged.
        The subscribe may have guessed (no row yet, or a row since narrowed to
        a team): the row's team is what the channel's ephemeral traffic is
        addressed to from now on, and when what the client was told about
        writing turns out wrong it is told again, from whichever path found
        out first."""
        settled = grant.settled(owner_user_id=owner_user_id, team_id=team_id, can_write=can_write)
        self.channels[grant.channel.key] = settled
        if settled.can_write != grant.can_write:
            await self.send(
                SubscribedFrame(channel=settled.channel.key, can_write=settled.can_write)
            )
        return settled

    async def _hello(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        async with AsyncSessionLocal() as db:
            try:
                snapshot = await self.registry.snapshot(
                    db,
                    grant=grant,
                    user=self.subject,
                    actor=self.actor,
                    ent=self.ent,
                    agent_id=self.machine_id,
                )
                await db.commit()
            except DocOpRejectedError as exc:
                await db.rollback()
                await self.send(self._doc_error(envelope, exc.code, exc.message))
                return
        grant = await self._settle(
            grant,
            owner_user_id=snapshot.owner_user_id,
            team_id=snapshot.team_id,
            can_write=snapshot.can_write,
        )
        await self.send(
            DocFrame(
                envelope=DocEnvelope(
                    doc_id=snapshot.doc_id,
                    doc_type=grant.channel.doc_type,
                    epoch=snapshot.epoch,
                    peer_id=SERVER_PEER_ID,
                    seq=snapshot.seq,
                    kind="snapshot",
                    payload=SnapshotPayload(state=snapshot.state, seq=snapshot.seq).model_dump(
                        mode="json"
                    ),
                )
            )
        )

    async def _op(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        async with AsyncSessionLocal() as db:
            try:
                applied = await self.registry.apply_op(
                    db,
                    grant=grant,
                    user=self.subject,
                    envelope=envelope,
                    actor=self.actor,
                    ent=self.ent,
                    agent_id=self.machine_id,
                )
                await db.commit()
            except StaleEpochError as exc:
                await db.rollback()
                await self.send(
                    self._doc_error(envelope, "stale_epoch", str(exc), epoch=exc.current_epoch)
                )
                await self.send(
                    DocFrame(
                        envelope=DocEnvelope(
                            doc_id=envelope.doc_id,
                            doc_type=envelope.doc_type,
                            epoch=exc.current_epoch,
                            peer_id=SERVER_PEER_ID,
                            seq=0,
                            kind="reload",
                            payload=ReloadPayload(
                                epoch=exc.current_epoch, reason="stale_epoch"
                            ).model_dump(mode="json"),
                        )
                    )
                )
                return
            except DocOpRejectedError as exc:
                await db.rollback()
                await self.send(self._doc_error(envelope, exc.code, exc.message))
                return
        op_id = str(envelope.payload.get("op_id", ""))
        await self.send(
            DocFrame(
                envelope=DocEnvelope(
                    doc_id=envelope.doc_id,
                    doc_type=envelope.doc_type,
                    epoch=applied.epoch,
                    peer_id=SERVER_PEER_ID,
                    seq=applied.seq,
                    kind="ack",
                    payload=AckPayload(
                        op_id=op_id, seq=applied.seq, changed=applied.changed
                    ).model_dump(mode="json"),
                )
            )
        )

    async def _chunk(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        wire = envelope.model_copy(update={"seq": 0})
        async with AsyncSessionLocal() as db:
            # The ephemeral lane never touches the row, but who may stream on
            # it is still the row's business: its publisher, and only once the
            # document exists.
            try:
                doc = await self.registry.locate(db, grant=grant, user=self.subject, ent=self.ent)
            except DocOpRejectedError as exc:
                await self.send(self._doc_error(envelope, exc.code, exc.message))
                return
            grant = await self._settle(
                grant,
                owner_user_id=doc.owner_user_id,
                team_id=doc.team_id,
                can_write=await self.registry.writable(
                    db, doc=doc, user=self.subject, ent=self.ent, agent_id=self.machine_id
                ),
            )
            # The ephemeral lane is the PUBLISHER's, not every writer's: a
            # frame on it is the machine's own streaming output, and a peer
            # merely shared the document at ``writer`` must not be able to put
            # words in the agent's mouth. ``can_write`` above is the wider
            # question the peer is told the answer to.
            if not await self.registry.publishes(
                db, doc=doc, user=self.subject, ent=self.ent, agent_id=self.machine_id
            ):
                await self.send(
                    self._doc_error(envelope, "forbidden", "only the publisher streams")
                )
                return
            events = envelope.payload.get("events")
            if not isinstance(events, list) or not events:
                await self.send(self._doc_error(envelope, "bad_op", "a chunk carries events"))
                return
            for event in events:
                if (
                    not isinstance(event, dict)
                    or event.get("event_type") not in NON_PERSISTED_EVENT_TYPES
                ):
                    await self.send(
                        self._doc_error(
                            envelope,
                            "unsupported_kind",
                            "a chunk carries only never-persisted events",
                        )
                    )
                    return
            try:
                await presence.publish_ephemeral(
                    db,
                    presence.ephemeral_event(
                        grant=grant,
                        type=presence.CHUNK_EVENT_TYPE,
                        body={"envelope": wire.model_dump(mode="json")},
                    ),
                )
            except presence.EphemeralTooLargeError as exc:
                await self.send(self._doc_error(envelope, "chunk_too_large", str(exc)))
                return
            await db.commit()

    async def _publisher_snapshot(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        state = envelope.payload.get("state")
        if not isinstance(state, dict):
            await self.send(
                self._doc_error(envelope, "bad_op", "a snapshot carries a state object")
            )
            return
        async with AsyncSessionLocal() as db:
            # Who may rebuild is the row's publisher: the registry decides.
            try:
                await self.registry.rebuild(
                    db,
                    grant=grant,
                    user=self.subject,
                    state=state,
                    reason=REBUILD_REASON_PUBLISHER,
                    actor=self.actor,
                    ent=self.ent,
                    agent_id=self.machine_id,
                )
                await db.commit()
            except DocOpRejectedError as exc:
                await db.rollback()
                await self.send(self._doc_error(envelope, exc.code, exc.message))

    # -- tick --------------------------------------------------------------

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        keepalive = float(settings.realtime_sse_keepalive_seconds)
        deadline = loop.time() + session_deadline_seconds(settings.realtime_ws_max_session_seconds)
        faults = BeatFaults()
        while not self.closed:
            await asyncio.sleep(keepalive)
            if loop.time() >= deadline:
                await self.close(close_codes.SESSION_EXPIRED, "session deadline")
                return
            try:
                verdict = await self._beat()
            except Exception as exc:  # the database, not the session, failed
                if not faults.tolerate(exc):
                    log.warning("realtime.ws.recheck_failed", error=str(exc))
                    await self.close(close_codes.SERVER_RESET, "recheck failed")
                    return
                log.warning("realtime.ws.beat_skipped", error=str(exc), skipped=faults.skipped)
                continue
            faults.landed()
            if verdict is not None:
                return await self.close(*verdict)
            if self.crdt is not None:
                try:
                    await self.crdt.renew()
                except Exception as exc:  # a missed renewal lapses into a fresh peer
                    log.warning("realtime.ws.crdt_renew_failed", error=str(exc))
                await self._reconsider_documents()
            await nb_reconsider(self.notebooks)

    async def _beat(self) -> tuple[int, str] | None:
        """One tick's database work and its close; the tick judges a raise."""
        if not await self._recheck():
            return close_codes.SESSION_EXPIRED, "session ended"
        if self.machine_id is not None and not await self._still_the_machine():
            # The machine is no longer one (credential revoked, row released):
            # the box reconnects, and its ticket mint says why.
            return close_codes.UNAUTHORIZED, "machine credential refused"
        await self._heartbeat_joined()
        return None


@dataclass(kw_only=True)
class SocketSession(_SocketCore):
    """A person's socket, admitted on a ticket minted from their session."""

    user: User
    claims: SessionClaims
    ref: EntitlementRef
    #: The agent the socket was opened as (its ticket's assertion); ``None``
    #: for a person's own socket. Names the actor on every row this socket
    #: writes; it decides nothing by itself.
    agent_id: str | None = None
    #: The workspace machine this socket was VERIFIED to speak as at
    #: admission — ``agent_id`` when it named a live machine of the org
    #: registered by this very user, else ``None`` (a person's socket, or an
    #: assertion that did not verify). This, never the bare assertion, is what
    #: the channel rules and the registry compare with a chat's binding: a
    #: machine's id is on every chat it serves, so a colleague putting it on
    #: their own ticket must read and write exactly what they would without it.
    verified_machine_id: str | None = None

    def __post_init__(self) -> None:
        # Which window this socket is held to is decided once, at admission,
        # from what the server PROVED the socket to be: a verified workspace
        # machine is the box publishing a chat, and nothing a client sends can
        # move itself into that lane.
        if self.verified_machine_id is not None:
            self._publisher_windows()
        docs = getattr(self.runtime, "crdt", None)
        if docs is not None:
            self.crdt = CrdtSocket(host=self, docs=docs)
        self.notebooks = notebook_socket_for(self)

    @property
    def subject(self) -> Subject:
        return self.user

    @property
    def org_id(self) -> UUID:  # the ticket's (its session's), never the home org
        return self.claims.org_team_id

    @property
    def ent(self) -> EntitlementSnapshot:
        return self.ref.value

    @property
    def machine_id(self) -> str | None:
        return self.verified_machine_id

    @property
    def label(self) -> str:
        return f"ws:{self.user.id}"

    @property
    def actor(self) -> Mapping[str, Any]:
        """The actor document on every row this socket writes: the user acting
        alone, or ``[user, agent]`` when the socket speaks as an agent — the
        same chain a REST call with the agent headers records."""
        if self.agent_id is None:
            return actor_for_user(self.user, org_id=self.org_id)
        return ActingContext.for_agent(
            user_id=self.user.id,
            org_id=self.org_id,
            email=self.user.email,
            session_id=self.agent_id,
        ).audit_dict()

    # -- outbound ----------------------------------------------------------

    def _accept_event(self, event: HubEvent) -> bool:
        """The frames this socket wants: the traffic of a channel it holds,
        plus the announcements it needs for itself. None of the latter is
        forwarded to the client — the portal's own event stream carries them —
        but they are the only signal that what this socket may read moved while
        it was open: a share made or revoked, the chat deleted out from under a
        viewer, or the viewer's own role, team or account changing. The socket
        has to hear them to stop telling a fresh editor they may not type, and
        to stop streaming a conversation the reader no longer holds."""
        if not visible_to(event, user_id=self.user.id, org_id=self.org_id, ent=self.ref.value):
            return False
        if reauthorizes(event, user_id=self.user.id):
            return bool(self.channels)
        return event.channel is not None and (
            event.channel in self.channels
            or event.channel in self.machine_channels
            or nb_holds(self.notebooks, event.channel)
        )

    def _reauthorizes(self, event: HubEvent) -> bool:
        return reauthorizes(event, user_id=self.user.id)

    # -- inbound -----------------------------------------------------------

    async def _machine_frame(self, text: str) -> None:
        """A frame on the machine channel's vocabulary.

        Only a verified machine speaks it: anyone else is answered in-band, as
        for any frame it may not send. The machine itself is held to the
        vocabulary — a ``machine.*`` frame this server cannot parse closes the
        socket, because the box is the one peer it belongs to and a malformed
        answer is a bug to surface, not a frame to wait for more of.

        An ack is published under the socket's own verified machine and org,
        whatever the frame says: a box can answer only for itself."""
        if self.verified_machine_id is None:
            await self.send(ErrorFrame(code="forbidden", message="machine frames are the box's"))
            return
        try:
            ack = parse_machine_frame(text)
        except ValidationError:
            await self.close(close_codes.PROTOCOL_ERROR, "bad machine frame")
            return
        async with AsyncSessionLocal() as db:
            await presence.publish_ephemeral(
                db, machine_event(self.org_id, self.verified_machine_id, ack)
            )
            await db.commit()

    async def _grant_for(
        self, channel: Channel | WorkspaceChannel
    ) -> tuple[PresenceGrant, list[PresencePeer]]:
        if isinstance(channel, WorkspaceChannel):
            async with AsyncSessionLocal() as db:
                held = await authorize_workspace(db, self.user, channel, org_id=self.org_id)
                return held, await presence.roster(db, channel=channel, org_id=held.org_id)
        if channel.doc_type in CRDT_DOC_TYPES:
            if self.crdt is None:
                raise ChannelError("crdt_unsupported", "this server runs no CRDT lane")
            grant = await self.crdt.grant(channel)
            async with AsyncSessionLocal() as db:
                peers = await presence.roster(db, channel=channel, org_id=grant.org_id)
            return grant, peers
        async with AsyncSessionLocal() as db:
            grant = await authorize(
                db,
                self.user,
                channel,
                ent=self.ref.value,
                agent_id=self.verified_machine_id,
                rungs=self.rungs,
            )
            peers = await presence.roster(db, channel=channel, org_id=grant.org_id)
        return grant, peers

    async def _reconsider_writing(self) -> None:
        """Something that can move what this socket may read happened. The
        connection forgets every rung it remembered and re-decides each chat
        and live document channel it holds (a live document by its type's own
        rules). One whose answer moved is told again with a fresh
        ``subscribed`` frame, so a member granted Can-edit can type without
        reconnecting and one whose grant was revoked loses it at once.

        A chat is private until shared, so the same signal can also mean the
        chat this socket is watching is gone — revoked, deleted, or trashed
        under its owner. The registry re-checks the row's org and team on every
        frame but cannot see a rung and does not hide a tombstone from a
        subscriber it already admitted, so a subscription that outlived its
        share or its chat would keep receiving the transcript: it is ended here
        instead — the channel is dropped, its presence left, and the client told
        the same ``not_found`` a fresh subscribe would now answer.

        The account itself is re-read first. A share survives its holder being
        deactivated or blocked, so re-deciding the channels alone would leave a
        person the org just switched off reading every chat they had open; that
        is not a channel answer, it is the end of the session, and it is the
        same close a ticket for a switched-off account is refused with.
        """
        if not await self._account_still_stands():
            return
        self.rungs.invalidate()
        for key, grant in list(self.channels.items()):
            await self._redecide(key, grant)

    async def _reconsider_documents(self, only: str | None = None) -> None:
        await reconsider_documents(self, only)

    async def _redecide(self, key: str, grant: PresenceGrant) -> None:
        await redecide(self, key, grant)

    async def _account_still_stands(self) -> bool:
        """Whether the person behind this socket is still someone the org
        serves: the row is there, active, unblocked, and the membership the
        ticket names still stands in the socket's org. ``False`` closes it."""
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.user.id)
            if (
                user is not None
                and user.is_active
                and not is_blocked(user)
                and await membership_stands(db, self.claims)
            ):
                return True
        await self.close(close_codes.UNAUTHORIZED, "session ended")
        return False

    def display_name(self) -> str:
        return self._display_name()

    def _display_name(self) -> str:
        """What the other readers of a channel see when this socket joins or
        leaves. The same rule the roster's own read applies, so a person does
        not change name between the frame that announced them and the frame
        that listed them."""
        return presence.display_name_of(self.user.first_name, self.user.last_name, self.user.email)

    async def _presence(self, raw: str, event: str) -> None:
        grant = self.channels.get(raw)
        if grant is None:
            await self.send(
                ErrorFrame(code="not_subscribed", message="subscribe first", channel=raw)
            )
            return
        if event == "leave":
            if raw in self.joined:
                await self._leave(grant)
            return
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as db:
            if event == "heartbeat" and raw in self.joined:
                alive = await presence.heartbeat(
                    db, channel=grant.channel, peer_id=self.peer_id, now=now
                )
                if not alive:
                    # Swept while we were quiet: re-join, and tell the channel so.
                    await presence.join(
                        db,
                        channel=grant.channel,
                        peer_id=self.peer_id,
                        user_id=self.user.id,
                        org_id=grant.org_id,
                        now=now,
                    )
                    event = "join"
            else:
                await presence.join(
                    db,
                    channel=grant.channel,
                    peer_id=self.peer_id,
                    user_id=self.user.id,
                    org_id=grant.org_id,
                    now=now,
                )
                event = "join"
            await presence.publish_ephemeral(
                db,
                presence.ephemeral_event(
                    grant=grant,
                    type=presence.PRESENCE_EVENT_TYPE,
                    body={
                        "event": event,
                        "peer": presence.presence_peer(
                            self.peer_id,
                            self.user.id,
                            now,
                            email=self.user.email or "",
                            display_name=self._display_name(),
                        ).model_dump(mode="json"),
                    },
                ),
            )
            await db.commit()
        self.joined.add(raw)

    async def _cursor(self, raw: str, cursor: PresenceCursor) -> None:
        """Where this peer's caret is, told to the channel and to nobody's
        table. A caret moves on every keystroke, so it rides the ephemeral
        lane exactly as a heartbeat's delta does — but writes nothing: the
        presence row is the peer's liveness, and a caret is no evidence of
        that a heartbeat is not already giving. A peer that has not joined
        has no face for the caret to belong to, so it is told rather than
        joined on its behalf."""
        grant = self.channels.get(raw)
        if grant is None:
            await self.send(
                ErrorFrame(code="not_subscribed", message="subscribe first", channel=raw)
            )
            return
        if raw not in self.joined:
            await self.send(ErrorFrame(code="not_joined", message="join first", channel=raw))
            return
        async with AsyncSessionLocal() as db:
            await presence.publish_ephemeral(
                db,
                presence.ephemeral_event(
                    grant=grant,
                    type=presence.PRESENCE_EVENT_TYPE,
                    body={
                        "event": "cursor",
                        "peer": presence.presence_peer(
                            self.peer_id,
                            self.user.id,
                            email=self.user.email or "",
                            display_name=self._display_name(),
                            cursor=cursor,
                        ).model_dump(mode="json"),
                    },
                ),
            )
            await db.commit()

    async def _leave(self, grant: PresenceGrant) -> None:
        self.joined.discard(grant.channel.key)
        with contextlib.suppress(Exception):
            async with AsyncSessionLocal() as db:
                await presence.leave(db, channel=grant.channel, peer_id=self.peer_id)
                await presence.publish_ephemeral(
                    db,
                    presence.ephemeral_event(
                        grant=grant,
                        type=presence.PRESENCE_EVENT_TYPE,
                        body={
                            "event": "leave",
                            "peer": presence.presence_peer(
                                self.peer_id,
                                self.user.id,
                                email=self.user.email or "",
                                display_name=self._display_name(),
                            ).model_dump(mode="json"),
                        },
                    ),
                )
                await db.commit()

    # -- tick --------------------------------------------------------------

    async def _recheck(self) -> bool:
        """The socket stays open until its own deadline while the session
        FAMILY behind the ticket's access token is alive: revocation (jti and
        family) is re-checked every tick, the access token's own ``exp`` only
        when there is no family to ask (the event stream makes the same check).

        Holding the socket open is NOT activity: the tick enforces both idle
        windows and slides them only when the person did something on the
        socket since the last one (an edit, a cursor move). Pings, presence
        heartbeats and server pushes go on in a tab nobody is at, and counting
        them would keep an unattended session alive to its absolute expiry,
        which no slide can move either way."""
        acted, self._acted = self._acted, False
        async with AsyncSessionLocal() as db:
            user = (
                await db.execute(select(User).where(User.id == self.user.id))
            ).scalar_one_or_none()
            if user is None or not user.is_active or is_blocked(user):
                return False
            try:
                await assert_token_active(db, self.claims, user, slide_idle=acted)
            except TokenRevokedError:
                return False
            if not await claims_stand(db, self.claims, user=user):
                return False
            alive = await family_alive(db, self.claims)
            if alive is False or (alive is None and time.time() >= self.claims.expires_at):
                return False
            if acted:
                await slide_family_idle(db, self.claims)
                # This session of its own would roll the slides back on close.
                await db.commit()
            self.ref.value = await load_entitlements(db, user, org_id=self.org_id)
        return True

    async def _still_the_machine(self) -> bool:
        """Re-ask, on the tick, the question the socket's admission answered:
        is this still the machine speaking? A socket is held for as long as
        ``realtime_ws_max_session_seconds``, so an answer taken once at
        admission would let a box the platform took away keep publishing for
        that long. One indexed read per tick, for a machine's socket only."""
        async with AsyncSessionLocal() as db:
            return await verify_machine_assertion(
                db,
                machine_id=self.verified_machine_id,
                org_id=self.org_id,
                operator_user_id=self.user.id,
                credential_id=self.claims.jti,
            )

    async def _heartbeat_joined(self) -> None:
        for key in list(self.joined):
            if key in self.channels:
                await self._presence(key, "heartbeat")


#: The entitlement snapshot a box holds: in no org, a member of no team, an
#: admin of nothing and no platform staff, whatever org a frame is in.
_NO_ENTITLEMENTS = EntitlementSnapshot(None, frozenset(), org_admin=False, platform=False)


@dataclass(kw_only=True)
class MachineSocketSession(_SocketCore):
    """A box's socket, admitted on a ticket minted from its own machine credential.

    Nobody is behind it, so nothing a person's socket does for a person
    happens here: no presence, no caret, no relayed message, no rung. What it
    holds is exactly the box's job — the document of every chat bound to its
    machine, as the chat's publisher, the live document of every notebook in
    a folder it holds, as a reader, and its own machine channel — inside the
    orgs its credential serves. Every operation is judged by the registry
    against the chat's binding at that moment, and the tick re-asks the
    credential's standing and re-reads the binding of every chat it holds, so
    a revoked credential is cut and a rebound chat is dropped within one tick.
    """

    ctx: ActingContext
    credential_id: UUID
    scope: MachineScopeRef
    #: The org each machine request this socket forwarded was published under,
    #: by request id. A box answers on the machine channel, and the drive that
    #: asked listens under the CHAT's org — which for a pool box is not the
    #: box's own — so the ack goes back where the request came from, and an
    #: ack naming a request this socket never forwarded goes nowhere.
    _forwarded: OrderedDict[str, UUID] = field(default_factory=OrderedDict, init=False)

    def __post_init__(self) -> None:
        # The box IS the publisher of every chat it holds; it is held to the
        # publisher's windows from its first frame.
        self._publisher_windows()

    @property
    def subject(self) -> Subject:
        return MachineSubject(self.ctx)

    @property
    def actor(self) -> Mapping[str, Any]:
        return self.ctx.audit_dict()

    @property
    def ent(self) -> EntitlementSnapshot:
        return _NO_ENTITLEMENTS

    @property
    def machine_id(self) -> str | None:
        return self.ctx.acting_principal.id

    @property
    def label(self) -> str:
        return f"ws:machine:{self.ctx.acting_principal.id}"

    # -- outbound ----------------------------------------------------------

    def _accept_event(self, event: HubEvent) -> bool:
        """The frames a box wants: the traffic of a channel it holds, and the
        doorbell of a chat bound (or being bound) to it — the one announcement
        that can move what it holds. Decided by the one predicate the box's
        event stream decides with, then by the channels this socket holds."""
        if not machine_visible_to(event, scope=self.scope.value):
            return False
        if machine_reauthorizes(event, scope=self.scope.value):
            return bool(self.channels)
        return event.channel is not None and (
            event.channel in self.channels or event.channel in self.machine_channels
        )

    def _reauthorizes(self, event: HubEvent) -> bool:
        return machine_reauthorizes(event, scope=self.scope.value)

    def _machine_request(self, event: HubEvent) -> machine_wire.AnyMachineRequest | None:
        request = super()._machine_request(event)
        if isinstance(request, MachineRequest):
            self._forwarded[str(request.request_id)] = event.org_id
            self._forwarded.move_to_end(str(request.request_id))
            while len(self._forwarded) > FORWARDED_REQUEST_MEMORY:
                self._forwarded.popitem(last=False)
        return request

    # -- inbound -----------------------------------------------------------

    async def _machine_frame(self, text: str) -> None:
        """A box's answer on its machine channel, published under the org of
        the request it answers. A ``machine.*`` frame this server cannot parse
        closes the socket, exactly as for a box on its operator's session; an
        ack naming a request this socket never forwarded is answered in-band
        and published nowhere — it could only ever be a probe or a bug."""
        try:
            ack = parse_machine_frame(text)
        except ValidationError:
            await self.close(close_codes.PROTOCOL_ERROR, "bad machine frame")
            return
        org_id = self._forwarded.get(str(ack.request_id))
        if org_id is None:
            await self.send(
                ErrorFrame(code="unknown_request", message="no such request was forwarded here")
            )
            return
        async with AsyncSessionLocal() as db:
            await presence.publish_ephemeral(
                db, machine_event(org_id, self.ctx.acting_principal.id, ack)
            )
            await db.commit()

    async def _grant_for(
        self, channel: Channel | WorkspaceChannel
    ) -> tuple[PresenceGrant, list[PresencePeer]]:
        return await box_channels.grant(self.runtime, self.ctx, self.scope, channel), []

    async def _doc_frames(self, event: HubEvent) -> list[Any] | None:
        return await box_channels.followed_frames(
            self.runtime, self.channels, event, own_peer=self.peer_id
        )

    async def _reconsider_writing(self) -> None:
        """A chat's doorbell rang for a chat this box holds or is being bound
        to, or the tick came round. Every held channel the box no longer holds
        (:func:`box_channels.rescope`) is dropped, its client told the same
        ``not_found`` a fresh subscribe would now answer. A chat still bound
        needs no re-telling: the box is its publisher or it is not there."""
        for key in await box_channels.rescope(self.ctx, self.scope, self.channels):
            await self._unsubscribe(key)
            await self.send(ErrorFrame(code="not_found", message="not_found", channel=key))

    async def _presence(self, raw: str, event: str) -> None:
        await self.send(
            ErrorFrame(code="forbidden", message="a machine has no presence", channel=raw)
        )

    async def _cursor(self, raw: str, cursor: PresenceCursor) -> None:
        await self.send(
            ErrorFrame(code="forbidden", message="a machine has no presence", channel=raw)
        )

    async def _leave(self, grant: PresenceGrant) -> None:
        self.joined.discard(grant.channel.key)

    # -- tick --------------------------------------------------------------

    async def _recheck(self) -> bool:
        # There is no session behind a box's socket; its standing is the
        # credential's, re-asked by ``_still_the_machine`` every tick.
        self._acted = False
        return True

    async def _still_the_machine(self) -> bool:
        """Re-ask, on the tick, whether the credential still stands behind the
        machine — the same rule that admitted the socket — and, while it does,
        re-read the orgs it serves and the chats bound to it."""
        async with AsyncSessionLocal() as db:
            ctx = await _machine_context_for(
                db,
                credential_id=self.credential_id,
                machine_id=UUID(self.ctx.acting_principal.id),
                org_id=self.ctx.org_id,
                org_bound=self.ctx.is_machine_worker,
            )
        if ctx is None:
            return False
        self.ctx = ctx
        await self._reconsider_writing()
        return True

    async def _heartbeat_joined(self) -> None:
        return None


async def run_socket(
    websocket: WebSocket,
    *,
    admission: Admission | MachineAdmission,
    runtime: RealtimeRuntime,
    registry: DocRegistry,
) -> None:
    """Accept an admitted socket, greet it, run it, clean up. The caller
    owns the capacity slot."""
    session: _SocketCore
    peer_id = mint_peer_id()
    if isinstance(admission, MachineAdmission):
        async with AsyncSessionLocal() as db:
            scope = await load_machine_scope(db, admission.ctx)
        await websocket.accept(subprotocol=WS_SUBPROTOCOL)
        bind_trace_id(new_trace_id())
        bind_log_context(
            user_id="",
            org_id=str(admission.ctx.org_id),
            peer_id=peer_id,
            agent_id="",
            machine_id=admission.machine_id,
        )
        session = MachineSocketSession(
            websocket=websocket,
            peer_id=peer_id,
            runtime=runtime,
            registry=registry,
            ctx=admission.ctx,
            credential_id=admission.ticket.credential_id,
            scope=MachineScopeRef(scope),
        )
    else:
        async with AsyncSessionLocal() as db:
            ent = await load_entitlements(db, admission.user, org_id=admission.claims.org_team_id)
            # The ticket's assertion counts as a machine only when it verifies as
            # one this user registered with the session the ticket was minted from
            # — the same read REST makes per request, made once per socket. A
            # daemon that verifies here keeps it for the socket's life; a colleague
            # forging the box's id, or the operator on a browser session, gets a
            # person's socket.
            machine_id = admission.agent_id
            if machine_id is not None and not await verify_machine_assertion(
                db,
                machine_id=machine_id,
                org_id=admission.claims.org_team_id,
                operator_user_id=admission.user.id,
                credential_id=admission.claims.jti,
            ):
                machine_id = None
        await websocket.accept(subprotocol=WS_SUBPROTOCOL)
        bind_trace_id(new_trace_id())
        bind_log_context(
            user_id=str(admission.user.id),
            org_id=str(admission.claims.org_team_id),
            peer_id=peer_id,
            agent_id=admission.agent_id or "",
        )
        session = SocketSession(
            websocket=websocket,
            user=admission.user,
            claims=admission.claims,
            peer_id=peer_id,
            runtime=runtime,
            registry=registry,
            ref=EntitlementRef(ent),
            agent_id=admission.agent_id,
            verified_machine_id=machine_id,
        )
    await session.send(
        WelcomeFrame(
            peer_id=peer_id,
            server_time=datetime.now(UTC),
            instance=INSTANCE_ID,
            min_client_generation=REALTIME_CLIENT_GENERATION,
            limits=session.limits(),
        )
    )
    await session.run()


__all__ = [
    "FRAME_WINDOW_SECONDS",
    "MAX_CHANNELS",
    "MAX_FRAMES_PER_WINDOW",
    "MAX_FRAME_BYTES",
    "Admission",
    "MachineAdmission",
    "MachineSocketSession",
    "Refusal",
    "SocketSession",
    "admit",
    "allowed_origins",
    "mint_peer_id",
    "origin_allowed",
    "refuse",
    "run_socket",
    "ticket_from_subprotocols",
]
