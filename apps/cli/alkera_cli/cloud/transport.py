"""The daemon's socket to the realtime gateway.

One :class:`CloudSocket` holds one connection and any number of document
handles. Connecting is: mint a single-use ticket over REST, then open the
socket offering ``["alkera-v1", "alkera-ticket.<ticket>"]`` — the credential
travels in the subprotocol list and never in the URL. Once the server's
``welcome`` names our peer id, every open document is (re)subscribed, greeted
with ``hello`` and adopted from the ``snapshot`` that answers; a ``reload`` or
an ``error{stale_epoch}`` on one of our ops marks the document not live and
greets it again, and ops queued meanwhile wait for the new epoch.

An op is answered by an ``ack`` or an ``error`` naming its ``op_id``, and only
an answer that names it settles it: an ``error`` for a chunk (which names the
chunk's own id) or for a hello (which names none) can never take a durable
op's place, so a transcript entry is never dropped on another lane's account —
an op the server never answers times out and its owner resends it. A dropped
socket rejects every unanswered op as ``disconnected``; the caller decides
whether to resend (an ``append`` is idempotent by ``event_id``, so the mirror
does).

A box that has registered also holds its **machine channel**,
``machine:<machine id>``, on which the drive asks it for things — today, a
file's bytes a reader is waiting on (``machine.request``). The channel is
subscribed only on a connection whose ticket asserted that machine: the
gateway admits it for the verified machine alone, and a ticket minted before
registration asserted a placeholder. Every request is handed to the handler
:meth:`CloudSocket.bind_machine` installed, and its answer (``machine.ack``)
goes back on the same socket. No bytes of any file ever travel here.

Reconnects follow :class:`~alkera_cli.host.backoff.ReconnectBackoff`: a
successful connect never resets the delay, it only starts the healthy clock
that decays it. Close codes decide the shape of the retry — ``4401``/``4408``
mint a fresh ticket, ``4413``/``4429`` wait an extra step, ``4403``/``4404``
are final and surface as ``fatal`` — and everything else (``4503``, ``1012``, a
network error) simply reconnects.

An open socket is never trusted on its word. Every ``ping_interval`` the box
sends a ``ping`` and expects SOME frame back (the ``pong`` at least) within
``pong_timeout``; a connection that stays open but says nothing — held by a
proxy to a process whose loops have stopped, or to one draining away — is
closed and reconnected on the same backoff as any drop. The transport's own
keepalive cannot tell: it is answered by the peer's websocket layer whether or
not the application behind it is still delivering.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
from alkera_core.schemas.realtime import (
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
    DocFrame,
    DocType,
    ErrorFrame,
    PresenceFrame,
    ResetFrame,
    SocketLimits,
    SubscribedFrame,
    WelcomeFrame,
    dump_frame,
    parse_server_frame,
)
from alkera_core.schemas.realtime import (
    frames as wire,
)
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.typing import Subprotocol

from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.host.backoff import ReconnectBackoff

logger = logging.getLogger(__name__)

# The gateway's close codes (the protocol descriptor names the same numbers).
CLOSE_UNAUTHORIZED = 4401
CLOSE_ORIGIN_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_SESSION_EXPIRED = 4408
CLOSE_FRAME_TOO_LARGE = 4413
CLOSE_TOO_MANY = 4429
CLOSE_SERVER_RESET = 4500
CLOSE_UNAVAILABLE = 4503
CLOSE_SERVICE_RESTART = 1012

#: Close codes after which reconnecting can never succeed.
FATAL_CLOSE_CODES = frozenset({CLOSE_ORIGIN_FORBIDDEN, CLOSE_NOT_FOUND})
#: Close codes that ask for a fresh ticket (every reconnect mints one anyway;
#: these are the ones the protocol names).
REMINT_CLOSE_CODES = frozenset({CLOSE_UNAUTHORIZED, CLOSE_SESSION_EXPIRED})
#: Close codes that mean "you are sending too much": the wait is doubled.
THROTTLE_CLOSE_CODES = frozenset({CLOSE_FRAME_TOO_LARGE, CLOSE_TOO_MANY})

DEFAULT_PRESENCE_INTERVAL_SECONDS = 15.0
#: How often the box asks the gateway's application loop whether it is still
#: there, and how long it waits for any frame back before it calls the
#: connection dead. Together they bound how long a silent socket is believed.
DEFAULT_PING_INTERVAL_SECONDS = 20.0
DEFAULT_PONG_TIMEOUT_SECONDS = 20.0
#: The close code the box sends a connection it gave up on for silence (the
#: 4000-4999 range is the application's own; the gateway names none of it).
CLOSE_NO_PONG = 4000
#: How long the box waits for its own close of a silent connection to finish
#: before it drops the transport under it: the peer that stopped answering
#: pings will not answer a close either.
SILENT_CLOSE_SECONDS = 2.0
#: The server's two realtime size settings, as a server that predates
#: ``welcome.limits.ephemeral_max_bytes`` / ``doc_max_bytes`` runs them. A
#: welcome that names them wins; these are only the fallback.
#:
#: A streamed chunk rides ``pg_notify``, and the server refuses a notification
#: over ``realtime_ephemeral_max_bytes``, measured on the ASCII-escaped compact
#: JSON of the whole notification. The box leaves ``CHUNK_ENVELOPE_HEADROOM``
#: of that for everything the server wraps around its events.
FALLBACK_EPHEMERAL_MAX_BYTES = 4096
CHUNK_ENVELOPE_HEADROOM = 1024
#: A hello is answered with the whole document in one frame, and a document may
#: grow to ``realtime_doc_max_bytes``. A receive cap below that made every long
#: chat permanently unopenable: the oversized snapshot was refused, the
#: connection dropped, the hello timed out, and the chat was marked refused.
FALLBACK_DOC_MAX_BYTES = 4 * 1024 * 1024
#: What the box will ACCEPT: twice the document ceiling leaves room for the
#: JSON envelope and escaping around a snapshot.
MAX_SNAPSHOT_BYTES = 2 * FALLBACK_DOC_MAX_BYTES


@dataclass(frozen=True, slots=True)
class RealtimeSizes:
    """The two sizes the box holds its realtime traffic to.

    ``chunk_max_bytes`` bounds the events of one streamed chunk (in the
    server's measure, see :func:`alkera_cli.cloud.mirror.chunk_size`), and
    ``receive_max_bytes`` is the largest frame the box accepts."""

    chunk_max_bytes: int
    receive_max_bytes: int


def realtime_sizes(limits: SocketLimits | None) -> RealtimeSizes:
    """The sizes a welcome's ``limits`` imply, each falling back on its own.

    The notify cap is honoured in both directions: the server drops a chunk
    over it. The receive cap only ever grows past the fallback, because
    accepting more than a document can reach is harmless and refusing an
    ordinary frame is not."""
    ephemeral = limits.ephemeral_max_bytes if limits is not None else None
    doc = limits.doc_max_bytes if limits is not None else None
    return RealtimeSizes(
        chunk_max_bytes=(ephemeral or FALLBACK_EPHEMERAL_MAX_BYTES) - CHUNK_ENVELOPE_HEADROOM,
        receive_max_bytes=max(MAX_SNAPSHOT_BYTES, 2 * (doc or 0)),
    )


FALLBACK_SIZES = realtime_sizes(None)
DEFAULT_OP_TIMEOUT_SECONDS = 30.0

SocketState = Literal["idle", "connecting", "connected", "down", "fatal", "stopped"]
ConnectFactory = Callable[..., Any]

#: The channel the drive addresses one machine on, and the frame it asks with.
MACHINE_CHANNEL_PREFIX = "machine:"
MACHINE_REQUEST_FRAME = "machine.request"


@dataclass(frozen=True)
class LaterAnswer:
    """An answer sent now and a second one sent when ``then`` completes: a
    request whose work outlasts the drive's ack window (a flush's push) is
    acknowledged at once and settled when the work ends. ``then`` runs off the
    socket's receive loop, so the connection keeps serving meanwhile."""

    now: Mapping[str, Any]
    then: Awaitable[Mapping[str, Any] | None]


#: What answers a request on the machine channel: the decoded frame in, the
#: frame to send back out (or a :class:`LaterAnswer`), or ``None`` to send nothing.
MachineHandler = Callable[[Mapping[str, Any]], Mapping[str, Any] | LaterAnswer | None]


class DocOpError(Exception):
    """An op was not applied: the server's code, ``stale_epoch``,
    ``disconnected`` (the socket dropped before the answer), ``timeout`` or
    ``closed`` (the handle was closed)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class DocOp:
    """A rebroadcast operation another peer made on a document."""

    intent: str
    payload: dict[str, Any]
    peer_id: str
    epoch: int
    seq: int
    ephemeral: bool

    @property
    def op_id(self) -> str:
        return str(self.payload.get("op_id", ""))

    @property
    def events(self) -> list[dict[str, Any]]:
        events = self.payload.get("events")
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


@dataclass
class _Outstanding:
    op_id: str
    future: asyncio.Future[dict[str, Any]]


OpListener = Callable[[DocOp], None]
SnapshotListener = Callable[[dict[str, Any], int, int], None]


@dataclass
class DocHandle:
    """One subscribed document. Created by :meth:`CloudSocket.open_doc`."""

    doc_type: DocType
    doc_id: str
    presence: bool
    _socket: CloudSocket = field(repr=False)
    epoch: int = 0
    seq: int = 0
    can_write: bool = False
    state: dict[str, Any] | None = None
    error: str | None = None
    live: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    hello_pending: bool = False
    joined: bool = False
    outstanding: deque[_Outstanding] = field(default_factory=deque)
    op_listeners: list[OpListener] = field(default_factory=list)
    snapshot_listeners: list[SnapshotListener] = field(default_factory=list)

    @property
    def channel(self) -> str:
        return f"doc:{self.doc_type}:{self.doc_id}"

    def has_outstanding(self, op_id: str) -> bool:
        """Whether a durable op with this id is still waiting for its answer."""
        return any(entry.op_id == op_id for entry in self.outstanding)

    def on_op(self, listener: OpListener) -> Callable[[], None]:
        self.op_listeners.append(listener)
        return lambda: self.op_listeners.remove(listener)

    def on_snapshot(self, listener: SnapshotListener) -> Callable[[], None]:
        self.snapshot_listeners.append(listener)
        return lambda: self.snapshot_listeners.remove(listener)

    async def wait_live(self, wait_seconds: float | None = None) -> bool:
        """Block until the document has a snapshot at the current epoch."""
        if wait_seconds is None:
            await self.live.wait()
            return True
        try:
            await asyncio.wait_for(self.live.wait(), wait_seconds)
        except TimeoutError:
            return False
        return True

    async def send_op(
        self,
        intent: str,
        *,
        events: Sequence[dict[str, Any]] = (),
        meta: dict[str, Any] | None = None,
        op_id: str | None = None,
        ack_timeout: float | None = DEFAULT_OP_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send a durable op at the current epoch and return its ack payload
        ``{op_id, seq, changed}``. Waits for the document to be live first;
        ``ack_timeout`` bounds both waits."""
        return await self._socket._send_op(
            self, intent, events=list(events), meta=meta or {}, op_id=op_id, ack_timeout=ack_timeout
        )

    async def send_chunk(self, events: Sequence[dict[str, Any]]) -> bool:
        """Send an ephemeral ``chunk`` op (never acked). ``False`` when the
        socket is not connected or the document not live — the chunk is dropped,
        which is the ephemeral lane's contract."""
        return await self._socket._send_chunk(self, list(events))

    def close(self) -> None:
        self._socket.close_doc(self)


class CloudSocket:
    """See the module docstring."""

    def __init__(
        self,
        rest: CloudRestClient,
        *,
        backoff: ReconnectBackoff | None = None,
        presence_interval: float = DEFAULT_PRESENCE_INTERVAL_SECONDS,
        ping_interval: float = DEFAULT_PING_INTERVAL_SECONDS,
        pong_timeout: float = DEFAULT_PONG_TIMEOUT_SECONDS,
        connect: ConnectFactory = ws_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        origin: str | None = None,
    ) -> None:
        self._rest = rest
        self._backoff = backoff or ReconnectBackoff(clock=clock)
        self._presence_interval = presence_interval
        self._ping_interval = ping_interval
        self._pong_timeout = pong_timeout
        self._connect = connect
        self._sleep = sleep
        self._clock = clock
        self._origin = origin
        self._docs: dict[str, DocHandle] = {}
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._presence_task: asyncio.Task[None] | None = None
        self._liveness_task: asyncio.Task[None] | None = None
        #: Set whenever the live connection carries a frame in; the liveness
        #: loop clears it as it pings and waits for it.
        self._heard = asyncio.Event()
        self._silent_closes = 0
        self._stopping = False
        self._state: SocketState = "idle"
        self._fatal_reason: str | None = None
        self._peer_id: str | None = None
        self._state_listeners: list[Callable[[SocketState], None]] = []
        self._presence_listeners: list[Callable[[PresenceFrame], None]] = []
        self._connect_count = 0
        #: The machine this box is registered as, and what answers the drive
        #: on its channel. ``None`` until registration names it.
        self._machine_id: str | None = None
        self._machine_handler: MachineHandler | None = None
        #: The second answers still being worked on, held so none is collected.
        self._later: set[asyncio.Task[None]] = set()
        #: The agent the live connection's ticket asserted, and the machine
        #: channel subscribed on it — both ``None`` between connections.
        self._connected_as: str | None = None
        self._machine_channel: str | None = None
        #: What the last welcome said about sizes; kept across a drop so the
        #: next connection opens with the receive cap the server last named.
        self._sizes = FALLBACK_SIZES

    # -- observability ------------------------------------------------------

    @property
    def state(self) -> SocketState:
        return self._state

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    @property
    def sizes(self) -> RealtimeSizes:
        """The sizes the server's last welcome named, or the fallback."""
        return self._sizes

    @property
    def peer_id(self) -> str | None:
        return self._peer_id

    @property
    def connect_count(self) -> int:
        """How many times the socket has come up (tests pin the reconnect path)."""
        return self._connect_count

    @property
    def silent_closes(self) -> int:
        """How many connections were given up on because they went silent."""
        return self._silent_closes

    @property
    def backoff(self) -> ReconnectBackoff:
        return self._backoff

    @property
    def bound_machine(self) -> str | None:
        """The machine the drive's requests are answered for, once bound."""
        return self._machine_id

    @property
    def machine_channel(self) -> str | None:
        """The machine channel subscribed on the live connection, if any."""
        return self._machine_channel

    def on_presence(self, listener: Callable[[PresenceFrame], None]) -> Callable[[], None]:
        """Hear the readers on the documents this socket holds: a join, a beat,
        a caret, a leave. The gateway sends a channel's presence to every
        socket subscribed to it, so it is heard without joining the roster."""
        self._presence_listeners.append(listener)
        return lambda: self._presence_listeners.remove(listener)

    def on_state(self, listener: Callable[[SocketState], None]) -> Callable[[], None]:
        self._state_listeners.append(listener)
        return lambda: self._state_listeners.remove(listener)

    def _set_state(self, state: SocketState) -> None:
        if self._state == state:
            return
        self._state = state
        for listener in list(self._state_listeners):
            try:
                listener(state)
            except Exception:
                logger.exception("cloud socket state listener failed")

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="cloud-socket")

    async def stop(self) -> None:
        self._stopping = True
        for doc in list(self._docs.values()):
            await self._leave(doc)
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._reject_all(DocOpError("closed", "the socket was stopped"))
        self._set_state("stopped")

    @property
    def rest(self) -> CloudRestClient:
        """The client that mints this socket's tickets — whose agent id every
        ticket, and so every socket, speaks as."""
        return self._rest

    async def rebind(self, rest: CloudRestClient) -> None:
        """Speak as ``rest`` from the next connection on.

        The daemon learns the machine id it publishes as when registration
        answers, which may be after the socket first came up; a ticket minted
        before then asserts the wrong agent and the gateway would treat the
        socket as a reader. So a live connection is closed here and the loop
        reconnects at once with a ticket the new client minted — every open
        document re-subscribes and re-hellos exactly as after any drop, and
        an op in flight is answered ``disconnected`` and resent by its owner.
        """
        self._rest = rest
        ws = self._ws
        if ws is not None and self._state == "connected":
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="rebind")

    def bind_machine(self, machine_id: str | None, handler: MachineHandler | None = None) -> None:
        """Answer the drive on ``machine:<machine_id>`` with ``handler``.

        Subscribed at once when the live connection's ticket already asserted
        this machine, and otherwise on the next connection that does — which
        is the one :meth:`rebind` opens, so the caller binds first and rebinds
        after. Asking for the channel on a connection that speaks as anyone
        else would only be refused. ``None`` lets the channel go.
        """
        self._machine_id = machine_id or None
        self._machine_handler = handler if machine_id else None
        if self._ws is not None and self._state == "connected":
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._settle_machine_channel())

    def open_doc(self, doc_type: DocType, doc_id: str, *, presence: bool = True) -> DocHandle:
        """Subscribe (now, or as soon as the socket is up) and return the handle."""
        handle = DocHandle(doc_type=doc_type, doc_id=doc_id, presence=presence, _socket=self)
        self._docs[handle.channel] = handle
        if self._ws is not None and self._state == "connected":
            asyncio.get_running_loop().create_task(self._subscribe(handle))
        return handle

    def close_doc(self, handle: DocHandle) -> None:
        if handle.closed:
            return
        handle.closed = True
        self._docs.pop(handle.channel, None)
        self._reject_doc(handle, DocOpError("closed", "the document was closed"))
        if self._ws is not None and self._state == "connected":
            asyncio.get_running_loop().create_task(self._unsubscribe(handle))

    # -- the connection loop --------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            extra_wait = 1.0
            self._set_state("connecting")
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                reason = exc.rcvd.reason if exc.rcvd is not None else ""
                logger.info("cloud socket closed: code=%s reason=%s", code, reason)
                if code in FATAL_CLOSE_CODES:
                    self._fatal(f"closed {code}: {reason or 'refused'}")
                    return
                if code in THROTTLE_CLOSE_CODES:
                    extra_wait = 2.0
            except CloudApiError as exc:
                if exc.unauthorized:
                    # The mint runs under the box's own bearer, and a box
                    # outlives the one it booted with. ``request`` has already
                    # re-read the credential file and tried again under
                    # anything newer it found; reaching here means the file
                    # says exactly what was just refused — a real revocation,
                    # not a rotation this process slept through. Anything else
                    # would have the box reconnecting on a dead token for ever
                    # while every chat on it hung.
                    self._fatal("this box's session was refused; run `alkera login` on it again")
                    return
                logger.warning("cloud socket ticket mint failed: %s", exc)
            except (OSError, httpx.HTTPError, InvalidHandshake, InvalidURI, TimeoutError) as exc:
                logger.warning("cloud socket connect failed: %s", exc)
            except Exception:
                logger.exception("cloud socket session failed")
            finally:
                await self._teardown_connection()
            if self._stopping:
                break
            self._set_state("down")
            delay = self._backoff.next_delay() * extra_wait
            logger.info("cloud socket reconnecting in %.1fs", delay)
            await self._sleep(delay)
        self._set_state("stopped")

    def _fatal(self, reason: str) -> None:
        self._fatal_reason = reason
        logger.error("cloud socket gave up: %s", reason)
        self._set_state("fatal")

    async def _session(self) -> None:
        # The agent this ticket asserts: a rebind while the mint is on the
        # wire changes the client, not what this connection speaks as.
        rest = self._rest
        ticket = await rest.mint_ticket()
        offered = [
            cast(Subprotocol, WS_SUBPROTOCOL),
            cast(Subprotocol, f"{WS_TICKET_SUBPROTOCOL_PREFIX}{ticket}"),
        ]
        kwargs: dict[str, Any] = {
            "subprotocols": offered,
            "max_size": self._sizes.receive_max_bytes,
        }
        if self._origin is not None:
            kwargs["origin"] = self._origin
        async with self._connect(self._rest.ws_url, **kwargs) as ws:
            self._ws = ws
            raw = await ws.recv()
            self._heard.set()
            welcome = parse_server_frame(raw)
            if not isinstance(welcome, WelcomeFrame):
                raise DocOpError(
                    "bad_welcome", f"expected welcome, got {getattr(welcome, 't', '?')}"
                )
            self._peer_id = welcome.peer_id
            self._sizes = realtime_sizes(welcome.limits)
            # A snapshot only ever answers a hello sent after this line, so
            # the live connection takes the welcome's cap before one arrives.
            ws.protocol.max_message_size = self._sizes.receive_max_bytes
            self._connected_as = rest.agent_id
            self._machine_channel = None
            self._connect_count += 1
            self._backoff.connected()
            self._set_state("connected")
            for doc in list(self._docs.values()):
                await self._subscribe(doc)
            await self._settle_machine_channel()
            self._presence_task = asyncio.create_task(
                self._presence_loop(), name="cloud-socket-presence"
            )
            self._liveness_task = asyncio.create_task(
                self._liveness_loop(ws), name="cloud-socket-liveness"
            )
            async for message in ws:
                self._heard.set()
                await self._on_message(message)
            # The server closed the socket without an exception (a normal
            # close code): surface it as the same ConnectionClosed every other
            # close arrives as, so one handler reads the code.
            raise ws.protocol.close_exc

    async def _teardown_connection(self) -> None:
        for task in (self._presence_task, self._liveness_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._presence_task = None
        self._liveness_task = None
        self._ws = None
        self._peer_id = None
        self._connected_as = None
        self._machine_channel = None
        for doc in self._docs.values():
            doc.live.clear()
            doc.hello_pending = False
            doc.joined = False
        self._reject_all(DocOpError("disconnected", "the live connection dropped"))

    async def _presence_loop(self) -> None:
        # Real time on purpose: ``sleep`` is injected for the RECONNECT wait a
        # test wants to skip; a heartbeat that skipped its interval would storm
        # the gateway's frame-rate cap.
        while True:
            await asyncio.sleep(self._presence_interval)
            self._backoff.decay()
            for doc in list(self._docs.values()):
                if doc.joined and self._ws is not None:
                    await self._send_frame(
                        wire.PresenceHeartbeatFrame(channel=doc.channel), ignore_errors=True
                    )

    async def _liveness_loop(self, ws: ClientConnection) -> None:
        """Ping the gateway's application and close a connection that stops
        answering. Real time, like the presence beat: the injected sleep is
        the reconnect wait's, and a ping that skipped its interval would
        storm the gateway's frame meter."""
        while True:
            await asyncio.sleep(self._ping_interval)
            if self._ws is not ws:
                return
            self._heard.clear()
            try:
                # The send and the answer share one window: a send that cannot
                # get out is as silent as a gateway that does not answer.
                async with asyncio.timeout(self._pong_timeout):
                    await self._send_frame(wire.PingFrame(), ignore_errors=True)
                    await self._heard.wait()
                continue
            except TimeoutError:
                pass
            logger.warning(
                "cloud socket: nothing heard for %.0fs after a ping; the connection is "
                "open but not delivering, so it is closed and reconnected",
                self._pong_timeout,
            )
            self._silent_closes += 1
            await self._close_silent(ws)
            return

    async def _close_silent(self, ws: ClientConnection) -> None:
        """Close ``ws``, and drop its transport when the close itself is not
        answered — the read loop then ends and the reconnect path runs."""
        try:
            await asyncio.wait_for(
                ws.close(code=CLOSE_NO_PONG, reason="no pong"), SILENT_CLOSE_SECONDS
            )
        except Exception:
            transport = getattr(ws, "transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.abort()

    # -- outbound frames ------------------------------------------------------

    async def _send_frame(self, frame: Any, *, ignore_errors: bool = False) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(dump_frame(frame))
        except (ConnectionClosed, OSError):
            if not ignore_errors:
                raise
            return False
        return True

    async def _subscribe(self, doc: DocHandle) -> None:
        if doc.closed:
            return
        await self._send_frame(wire.SubscribeFrame(channel=doc.channel), ignore_errors=True)

    async def _settle_machine_channel(self) -> None:
        """Hold exactly the machine channel this connection may hold.

        That is ``machine:<id>`` when a machine is bound AND this connection's
        ticket asserted it, and nothing otherwise; a channel for a machine no
        longer bound is let go.
        """
        wanted: str | None = None
        if self._machine_id is not None and self._connected_as == self._machine_id:
            wanted = f"{MACHINE_CHANNEL_PREFIX}{self._machine_id}"
        held = self._machine_channel
        if held == wanted or self._ws is None:
            return
        if held is not None:
            self._machine_channel = None
            await self._send_frame(wire.UnsubscribeFrame(channel=held), ignore_errors=True)
        if wanted is not None and await self._send_frame(
            wire.SubscribeFrame(channel=wanted), ignore_errors=True
        ):
            self._machine_channel = wanted

    async def _on_machine_request(self, message: str | bytes) -> None:
        """Hand a request on the machine channel to its handler, and send the
        answer back. A request the handler cannot answer goes unanswered —
        the drive's own deadline covers it — and a handler that fails is
        logged, never allowed to take the connection down with it."""
        handler = self._machine_handler
        if handler is None or self._machine_channel is None:
            logger.warning("cloud socket: a machine request arrived with no machine channel held")
            return
        try:
            decoded = json.loads(message)
        except ValueError:
            return
        if not isinstance(decoded, dict):
            return
        try:
            answer = handler(decoded)
        except Exception:
            logger.exception("cloud socket: the machine request handler failed")
            return
        if isinstance(answer, LaterAnswer):
            task = asyncio.get_running_loop().create_task(self._answer_later(answer.then))
            self._later.add(task)
            task.add_done_callback(self._later.discard)
            answer = answer.now
        await self._send_answer(answer)

    async def _answer_later(self, then: Awaitable[Mapping[str, Any] | None]) -> None:
        try:
            answer = await then
        except Exception:
            logger.exception("cloud socket: a machine request's later answer failed")
            return
        await self._send_answer(answer)

    async def _send_answer(self, answer: Mapping[str, Any] | None) -> None:
        ws = self._ws
        if answer is None or ws is None:
            return
        with contextlib.suppress(ConnectionClosed, OSError):
            await ws.send(json.dumps(dict(answer), separators=(",", ":")))

    async def _unsubscribe(self, doc: DocHandle) -> None:
        if doc.joined:
            await self._send_frame(wire.PresenceLeaveFrame(channel=doc.channel), ignore_errors=True)
            doc.joined = False
        await self._send_frame(wire.UnsubscribeFrame(channel=doc.channel), ignore_errors=True)

    async def _leave(self, doc: DocHandle) -> None:
        if doc.joined and self._ws is not None:
            await self._send_frame(wire.PresenceLeaveFrame(channel=doc.channel), ignore_errors=True)
            doc.joined = False

    def _envelope(self, doc: DocHandle, kind: str, payload: dict[str, Any], *, epoch: int) -> Any:
        return DocFrame.model_validate(
            {
                "envelope": {
                    "doc_id": doc.doc_id,
                    "doc_type": doc.doc_type,
                    "epoch": epoch,
                    "peer_id": self._peer_id or "p:pending",
                    "seq": 0,
                    "kind": kind,
                    "payload": payload,
                }
            }
        )

    async def _hello(self, doc: DocHandle) -> None:
        if doc.closed or doc.hello_pending or self._ws is None:
            return
        doc.hello_pending = True
        doc.live.clear()
        await self._send_frame(self._envelope(doc, "hello", {}, epoch=0), ignore_errors=True)

    async def _send_op(
        self,
        doc: DocHandle,
        intent: str,
        *,
        events: list[dict[str, Any]],
        meta: dict[str, Any],
        op_id: str | None,
        ack_timeout: float | None,
    ) -> dict[str, Any]:
        if doc.closed:
            raise DocOpError("closed", "the document was closed")
        if not await doc.wait_live(ack_timeout):
            raise DocOpError("timeout", "the document did not come live in time")
        if self._ws is None:
            raise DocOpError("disconnected", "the live connection dropped")
        chosen = op_id or f"op-{secrets.token_hex(6)}"
        payload = {"op_id": chosen, "intent": intent, "events": events, "meta": meta}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        doc.outstanding.append(_Outstanding(op_id=chosen, future=future))
        try:
            await self._send_frame(self._envelope(doc, "op", payload, epoch=doc.epoch))
        except (ConnectionClosed, OSError) as exc:
            self._settle(doc, chosen, error=DocOpError("disconnected", str(exc)))
        try:
            if ack_timeout is None:
                return await future
            return await asyncio.wait_for(future, ack_timeout)
        except TimeoutError as exc:
            self._settle(doc, chosen, error=DocOpError("timeout", "no ack in time"))
            raise DocOpError("timeout", "no ack in time") from exc

    async def _send_chunk(self, doc: DocHandle, events: list[dict[str, Any]]) -> bool:
        if doc.closed or not doc.live.is_set() or self._ws is None:
            return False
        payload = {
            "op_id": f"chunk-{secrets.token_hex(4)}",
            "intent": "chunk",
            "events": events,
            "meta": {},
        }
        return await self._send_frame(
            self._envelope(doc, "op", payload, epoch=doc.epoch), ignore_errors=True
        )

    # -- inbound frames -------------------------------------------------------

    async def _on_message(self, message: str | bytes) -> None:
        try:
            frame = parse_server_frame(message)
        except ValidationError:
            logger.warning("cloud socket: unparseable frame")
            return
        if isinstance(frame, SubscribedFrame | ErrorFrame) and (
            frame.channel is not None and frame.channel.startswith(MACHINE_CHANNEL_PREFIX)
        ):
            # Ahead of the documents' own branch: no document is named
            # ``machine:``, so that branch would drop the gateway's answer.
            self._on_machine_channel_answer(frame)
        elif isinstance(frame, SubscribedFrame):
            doc = self._docs.get(frame.channel)
            if doc is None:
                return
            doc.can_write = frame.can_write
            doc.error = None
            if doc.presence and not doc.joined:
                if await self._send_frame(
                    wire.PresenceJoinFrame(channel=doc.channel), ignore_errors=True
                ):
                    doc.joined = True
            if not doc.live.is_set():
                await self._hello(doc)
        elif isinstance(frame, DocFrame):
            await self._on_envelope(frame)
        elif isinstance(frame, wire.RawFrame) and frame.t == MACHINE_REQUEST_FRAME:
            await self._on_machine_request(message)
        elif isinstance(frame, ErrorFrame):
            if frame.channel is not None:
                doc = self._docs.get(frame.channel)
                if doc is not None:
                    doc.error = frame.code
                    doc.hello_pending = False
                    self._reject_doc(doc, DocOpError(frame.code, frame.message))
            else:
                logger.warning("cloud socket error frame: %s %s", frame.code, frame.message)
        elif isinstance(frame, ResetFrame):
            for doc in list(self._docs.values()):
                doc.hello_pending = False
                await self._hello(doc)
        elif isinstance(frame, PresenceFrame):
            for listener in list(self._presence_listeners):
                try:
                    listener(frame)
                except Exception:
                    logger.exception("presence listener failed for %s", frame.channel)
        elif isinstance(frame, wire.PongFrame | wire.RawFrame):
            return

    def _on_machine_channel_answer(self, frame: SubscribedFrame | ErrorFrame) -> None:
        """The gateway's answer to the machine channel's subscribe: held, or
        refused (and then not counted as held)."""
        if isinstance(frame, ErrorFrame):
            logger.warning(
                "cloud socket: the machine channel %s was refused: %s %s",
                frame.channel,
                frame.code,
                frame.message,
            )
            if frame.channel == self._machine_channel:
                self._machine_channel = None
            return
        logger.info("cloud socket: holding the machine channel %s", frame.channel)

    async def _on_envelope(self, frame: DocFrame) -> None:
        envelope = frame.envelope
        doc = self._docs.get(envelope.channel)
        if doc is None:
            return
        payload = envelope.payload
        if envelope.kind == "snapshot":
            doc.hello_pending = False
            doc.epoch = envelope.epoch
            seq = payload.get("seq")
            doc.seq = seq if isinstance(seq, int) else envelope.seq
            state = payload.get("state")
            doc.state = dict(state) if isinstance(state, dict) else {}
            doc.error = None
            doc.live.set()
            for listener in list(doc.snapshot_listeners):
                try:
                    listener(doc.state, doc.epoch, doc.seq)
                except Exception:
                    logger.exception("snapshot listener failed for %s", doc.channel)
        elif envelope.kind == "ack":
            op_id = str(payload.get("op_id", ""))
            seq = payload.get("seq")
            if isinstance(seq, int) and payload.get("changed") is True:
                doc.seq = max(doc.seq, seq)
            self._settle(doc, op_id, ack=dict(payload))
        elif envelope.kind == "error":
            code = str(payload.get("code") or "error")
            message = str(payload.get("message") or "")
            named = payload.get("op_id")
            if isinstance(named, str) and named and doc.has_outstanding(named):
                # A verdict on one durable op, named by its id: only that op
                # is settled. A chunk's error names the chunk's own id and a
                # hello's names none, so neither can ever take a durable op's
                # place — an op the server never answers waits for its ack
                # timeout and is resent by its owner.
                self._settle(doc, named, error=DocOpError(code, message))
            elif doc.hello_pending and not named:
                # The hello itself was refused (not_found / quota_exceeded):
                # nothing will be delivered until the next hello.
                doc.hello_pending = False
                doc.error = code
            else:
                logger.info(
                    "cloud socket: %s error on %s not attributed to a durable op (op_id=%r): %s",
                    code,
                    doc.channel,
                    named,
                    message,
                )
            if code == "stale_epoch" and envelope.epoch > doc.epoch:
                doc.live.clear()
        elif envelope.kind == "reload":
            target = payload.get("epoch")
            if isinstance(target, int) and target <= doc.epoch and doc.live.is_set():
                return
            doc.live.clear()
            doc.hello_pending = False
            await self._hello(doc)
        elif envelope.kind == "op":
            if envelope.epoch < doc.epoch:
                return
            if envelope.epoch > doc.epoch and doc.live.is_set():
                # We missed a reload: resync.
                doc.live.clear()
                doc.hello_pending = False
                await self._hello(doc)
                return
            intent = payload.get("intent")
            if not isinstance(intent, str):
                return
            op = DocOp(
                intent=intent,
                payload=dict(payload),
                peer_id=envelope.peer_id,
                epoch=envelope.epoch,
                seq=envelope.seq,
                ephemeral=envelope.seq == 0,
            )
            if not op.ephemeral:
                doc.seq = max(doc.seq, envelope.seq)
            for op_listener in list(doc.op_listeners):
                try:
                    op_listener(op)
                except Exception:
                    logger.exception("op listener failed for %s", doc.channel)

    # -- settling ---------------------------------------------------------------

    def _settle(
        self,
        doc: DocHandle,
        op_id: str,
        *,
        ack: dict[str, Any] | None = None,
        error: DocOpError | None = None,
    ) -> None:
        for entry in list(doc.outstanding):
            if entry.op_id == op_id:
                doc.outstanding.remove(entry)
                if not entry.future.done():
                    if error is not None:
                        entry.future.set_exception(error)
                    else:
                        entry.future.set_result(ack or {})
                return

    def _reject_doc(self, doc: DocHandle, error: DocOpError) -> None:
        while doc.outstanding:
            entry = doc.outstanding.popleft()
            if not entry.future.done():
                entry.future.set_exception(error)

    def _reject_all(self, error: DocOpError) -> None:
        for doc in self._docs.values():
            self._reject_doc(doc, error)


__all__ = [
    "CHUNK_ENVELOPE_HEADROOM",
    "CLOSE_FRAME_TOO_LARGE",
    "CLOSE_NOT_FOUND",
    "CLOSE_NO_PONG",
    "CLOSE_ORIGIN_FORBIDDEN",
    "CLOSE_SERVER_RESET",
    "CLOSE_SERVICE_RESTART",
    "CLOSE_SESSION_EXPIRED",
    "CLOSE_TOO_MANY",
    "CLOSE_UNAUTHORIZED",
    "CLOSE_UNAVAILABLE",
    "DEFAULT_OP_TIMEOUT_SECONDS",
    "DEFAULT_PING_INTERVAL_SECONDS",
    "DEFAULT_PONG_TIMEOUT_SECONDS",
    "DEFAULT_PRESENCE_INTERVAL_SECONDS",
    "FALLBACK_DOC_MAX_BYTES",
    "FALLBACK_EPHEMERAL_MAX_BYTES",
    "FALLBACK_SIZES",
    "FATAL_CLOSE_CODES",
    "MACHINE_CHANNEL_PREFIX",
    "MACHINE_REQUEST_FRAME",
    "MAX_SNAPSHOT_BYTES",
    "REMINT_CLOSE_CODES",
    "THROTTLE_CLOSE_CODES",
    "CloudSocket",
    "DocHandle",
    "DocOp",
    "DocOpError",
    "LaterAnswer",
    "MachineHandler",
    "RealtimeSizes",
    "SocketState",
    "realtime_sizes",
]
