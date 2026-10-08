"""The Loro CRDT lane on one socket.

A person's socket hands every frame on a CRDT channel (``doc:<type>:<id>`` for
every type the registry serves: the chat's draft, a file) to its
:class:`CrdtSocket`, which speaks the lane's protocol:

* ``hello`` — claim a Loro peer for this socket when it may write (the one the
  client offers back when it still holds the document it wrote with it, else a
  fresh one; a reader is handed an id that is never stored), and answer with
  ``snapshot`` carrying what the client is missing, chunked when it does not
  fit one frame. Each hello locks the document row, so a socket is held to
  :data:`HELLO_BURST` of them and then :data:`HELLO_PER_SECOND` per channel;
* ``crdt{t: update}`` — reassemble it when chunked, have the store judge and
  commit it, and answer ``ack`` with the server's version vector after the
  commit (or ``error`` with the update's id, and on ``stale_epoch`` a
  ``reload`` naming the current epoch);
* ``crdt{t: ephemeral}`` — a caret: checked by the sandbox, stamped with who
  sent it, relayed to the channel's other sockets and stored nowhere.

On the way out it drops this socket's own updates (it was answered with an
ack), reads a delta announced by reference from the log, and chunks anything
over one frame. It keeps the Loro peers it holds renewed on the tick and
releases them when the channel or the socket goes.

A socket that crashes the validator again and again is closed: one bad update
is a refusal, three in ten minutes is somebody trying. Only crashes count: an
update that outlived its budget is answered busy, and costs nobody a strike.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import UUID

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_PROTOCOL,
    SERVER_PEER_ID,
    CrdtAckPayload,
    CrdtChunk,
    CrdtEphemeralPayload,
    CrdtGonePayload,
    CrdtHelloPayload,
    CrdtLimits,
    CrdtSavingPayload,
    CrdtSyncMode,
    CrdtSyncPayload,
    CrdtUpdatePayload,
    DocEnvelope,
    DocFrame,
    EnvelopeKind,
    ErrorPayload,
    RawCrdtPayload,
    ReloadPayload,
    decode_b64,
    encode_b64,
    parse_crdt_payload,
)
from pydantic import ValidationError
from sqlalchemy import text

from backend.services.crdt.chunks import ChunkAssembler, ChunkError, split
from backend.services.crdt.docs import CRDT_UPDATE_EVENT_TYPE, CrdtDocs, CrdtError, safe_error
from backend.services.crdt.errors import busy_for
from backend.services.crdt.registry import DocRef
from backend.services.crdt.switch import LIVE_EDITING_OFF, OFF_MESSAGE, covers
from backend.services.realtime import close_codes, presence
from backend.services.realtime.channels import Channel, ChannelError, ChannelGrant
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.realtime.runtime import INSTANCE_ID
from backend.services.sharing import SharedRungCache

log = get_logger(__name__)

#: The hub event a relayed caret becomes on every replica.
CRDT_EPHEMERAL_EVENT_TYPE = "doc.crdt_ephemeral"
#: The key of a caret event's body naming the notebook cell the caret is in.
CARET_CELL_KEY = "caret_cell"
#: Validator crashes one socket may cause in the window before it is closed.
MAX_VALIDATOR_CRASHES = 3
VALIDATOR_CRASH_WINDOW_SECONDS = 600.0
#: Hellos one socket may send on one channel at once, and how fast that refills.
HELLO_BURST = 3
HELLO_PER_SECOND = 1.0


class CrdtHost(Protocol):
    """What the lane needs from the socket it runs on."""

    peer_id: str
    user: User
    agent_id: str | None
    rungs: SharedRungCache

    @property
    def org_id(self) -> UUID:
        """The org the socket's credential is for: the org of every document
        the lane opens for it."""
        ...

    @property
    def machine_id(self) -> str | None:
        """The machine the socket was VERIFIED to speak as; ``agent_id`` is
        only what it asserted."""
        ...

    @property
    def ent(self) -> EntitlementSnapshot: ...

    @property
    def actor(self) -> Mapping[str, Any]: ...

    async def send(self, frame: Any) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...

    def display_name(self) -> str: ...


@dataclass(slots=True)
class _Held:
    """This socket's state on one CRDT channel once it said ``hello``."""

    ref: DocRef
    loro_peer: int
    epoch: int
    assembler: ChunkAssembler
    #: The channel's grant: who the socket's announcements are addressed to.
    grant: ChannelGrant
    #: Every peer this person may write the document as, read at the hello.
    peers: frozenset[int] = frozenset()
    #: Whether ``loro_peer`` is a stored, held writing peer; a reader's is
    #: neither, so it is never released, renewed or announced gone.
    writer: bool = True


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: float


def _envelope(channel: Channel, *, epoch: int, kind: str, payload: Mapping[str, Any]) -> DocFrame:
    return DocFrame(
        envelope=DocEnvelope(
            doc_id=channel.doc_id,
            doc_type=channel.doc_type,
            epoch=max(epoch, 1),
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind=cast(EnvelopeKind, kind),
            payload=dict(payload),
        )
    )


@dataclass
class CrdtSocket:
    host: CrdtHost
    docs: CrdtDocs
    clock: Callable[[], float] = time.monotonic
    hello_burst: int = HELLO_BURST
    hello_per_second: float = HELLO_PER_SECOND
    _held: dict[str, _Held] = field(default_factory=dict, init=False)
    _hellos: dict[str, _Bucket] = field(default_factory=dict, init=False)
    _crashes: deque[float] = field(default_factory=deque, init=False)
    #: The channels whose hello is being answered, each with the events
    #: announced on it meanwhile, sent after the answer (:meth:`_hello`).
    _answering: dict[str, list[HubEvent]] = field(default_factory=dict, init=False)

    @property
    def holder(self) -> str:
        """Who holds a Loro peer: this socket, on this process."""
        return f"{INSTANCE_ID}:{self.host.peer_id}"

    def _ref(self, channel: Channel) -> DocRef:
        return DocRef(org_id=self.host.org_id, doc_type=channel.doc_type, doc_id=channel.doc_id)

    # -- subscribe -----------------------------------------------------------

    async def grant(self, channel: Channel) -> ChannelGrant:
        """The socket's grant on a CRDT channel, or ``ChannelError``:
        ``crdt_unsupported`` when the document cannot be served (live editing
        is off for its org, among others), ``not_found`` when it may not
        read. The socket's tick asks again for every held document with a
        source, so a switch turned off ends a held channel on that tick."""
        ref = self._ref(channel)
        async with AsyncSessionLocal() as db:
            try:
                access = await self.docs.access(
                    db,
                    ref,
                    user=self.host.user,
                    ent=self.host.ent,
                    agent_id=self.host.agent_id,
                    machine_id=self.host.machine_id,
                    rungs=self.host.rungs,
                )
            except CrdtError as exc:
                if exc.code == "crdt_unsupported":
                    raise ChannelError("crdt_unsupported", exc.message) from exc
                raise ChannelError("not_found") from exc
        if covers(self.docs.type_of(ref)) and not await self.docs.switch.on(ref.org_id):
            raise ChannelError("crdt_unsupported", OFF_MESSAGE)
        return ChannelGrant(
            channel=channel,
            org_id=self.host.org_id,
            can_write=access.can_write,
            owner_user_id=access.owner_user_id,
            team_id=access.team_id,
            visibility=access.visibility,
        )

    async def drop(self, key: str) -> None:
        """The socket left ``key``: give its Loro peer back, and tell the
        document's other tabs, so this one's caret goes now rather than when
        it times out there."""
        held = self._held.pop(key, None)
        if held is None or not held.writer:
            return
        try:
            async with AsyncSessionLocal() as db:
                await self.docs.release_peer(db, peer=held.loro_peer, holder=self.holder)
                await db.commit()
        except Exception as exc:  # the hold lapses on its own
            log.warning("crdt.peer.release_failed", error=safe_error(exc))
        # A document with a source is written back when a writer leaves.
        self.docs.left(held.ref)
        await self._announce_gone(held)

    async def _announce_gone(self, held: _Held) -> None:
        """Best effort: a tab that misses this still drops the caret when it
        stops being refreshed."""
        channel = held.grant.channel
        envelope = DocEnvelope(
            doc_id=channel.doc_id,
            doc_type=channel.doc_type,
            epoch=max(held.epoch, 1),
            peer_id=self.host.peer_id,
            seq=0,
            kind="crdt",
            payload=CrdtGonePayload(loro_peer=held.loro_peer).model_dump(mode="json"),
        )
        try:
            async with AsyncSessionLocal() as db:
                # A caret is gone the moment it is superseded: its NOTIFY
                # need not wait for the WAL, so its turn at the database's
                # notify lock stays short.
                await db.execute(text("SET LOCAL synchronous_commit = off"))
                await presence.publish_ephemeral(
                    db,
                    presence.ephemeral_event(
                        grant=held.grant,
                        type=CRDT_EPHEMERAL_EVENT_TYPE,
                        body={"envelope": envelope.model_dump(mode="json")},
                    ),
                )
                await db.commit()
        except Exception as exc:
            log.warning("crdt.caret.gone_failed", error=safe_error(exc))

    async def drop_all(self) -> None:
        for key in list(self._held):
            await self.drop(key)

    async def renew(self) -> None:
        """Keep every held Loro peer this socket's, on the tick, and give
        each held document with a source its periodic look for an outside
        change (the announcement of one can be missed)."""
        if not self._held:
            return
        async with AsyncSessionLocal() as db:
            for held in self._held.values():
                if held.writer:
                    await self.docs.renew_peer(db, peer=held.loro_peer, holder=self.holder)
            await db.commit()
        for held in self._held.values():
            self.docs.check_source(held.ref)

    def observe(self, event: HubEvent) -> None:
        """An announcement that can mean a held document's source changed
        outside its session (a file's node changed): that document looks."""
        for held in self._held.values():
            source = self.docs.type_of(held.ref).source
            if source is not None and source.changed(event.type, event.payload) == held.ref.doc_id:
                self.docs.check_source(held.ref)

    # -- inbound -------------------------------------------------------------

    async def handle(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        """One frame on a CRDT channel. Whatever goes wrong handling it is that
        channel's refusal, never the socket's end: the person's other channels
        (the chat itself, its presence) stay up."""
        try:
            await self._handle(grant, envelope)
        except Exception as exc:
            await self._error(
                grant.channel,
                envelope,
                _refusal_for(exc, channel=grant.channel.key, kind=envelope.kind),
                update_id=(
                    str(envelope.payload.get("update_id"))[:64]
                    if isinstance(envelope.payload.get("update_id"), str)
                    else None
                ),
            )

    async def _handle(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        if envelope.kind == "hello":
            await self._hello(grant, envelope)
        elif envelope.kind == "crdt":
            await self._crdt(grant, envelope)
        else:
            await self._error(
                grant.channel,
                envelope,
                CrdtError("unsupported_kind", f"a client may not send {envelope.kind!r} here"),
            )

    async def _error(
        self,
        channel: Channel,
        envelope: DocEnvelope,
        exc: CrdtError,
        *,
        update_id: str | None = None,
    ) -> None:
        await self.host.send(
            _envelope(
                channel,
                epoch=exc.epoch if exc.epoch is not None else envelope.epoch,
                kind="error",
                payload=ErrorPayload(
                    code=exc.code,
                    message=exc.message[:500],
                    update_id=update_id,
                    reason=exc.reason,
                    retry_after_ms=exc.retry_after_ms,
                ).model_dump(mode="json"),
            )
        )
        if exc.code == "stale_epoch" and exc.epoch is not None:
            await self.host.send(
                _envelope(
                    channel,
                    epoch=exc.epoch,
                    kind="reload",
                    payload=ReloadPayload(
                        epoch=exc.epoch, reason="stale_epoch", saving=exc.saving
                    ).model_dump(mode="json"),
                )
            )

    async def _hello(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        """Answer a hello, and only then send what was announced on its
        channel while it was being answered. The answer is what the document
        held when it was read; an edit committed after that read is not in it,
        and its broadcast, sent ahead of the answer, would reach a tab with no
        document to apply it to (or the one the answer is about to replace)
        and be lost to it. Sent after, it lands on the answer."""
        key = grant.channel.key
        held_back = self._answering.setdefault(key, [])
        try:
            await self._answer_hello(grant, envelope)
        finally:
            while held_back:
                for frame in await self._frames(held_back.pop(0)):
                    await self.host.send(frame)
            self._answering.pop(key, None)

    async def _answer_hello(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        channel = grant.channel
        try:
            hello = CrdtHelloPayload.model_validate(envelope.payload)
        except ValidationError:
            await self._error(channel, envelope, CrdtError("bad_hello", "not a CRDT hello"))
            return
        if hello.proto != CRDT_PROTOCOL:
            await self._error(
                channel,
                envelope,
                CrdtError("crdt_unsupported", f"this server speaks protocol {CRDT_PROTOCOL}"),
            )
            return
        ref = self._ref(channel)
        doc_type = self.docs.type_of(ref)
        if hello.doc_schema < doc_type.doc_schema:
            await self._error(
                channel,
                envelope,
                CrdtError("crdt_unsupported", "this client is older than the document"),
            )
            return
        if covers(doc_type) and not await self.docs.switch.on(ref.org_id):
            # Subscribed before the switch went off: the tab shows the file
            # another way, and its tick ends the channel.
            off = CrdtError("crdt_unsupported", OFF_MESSAGE, reason=LIVE_EDITING_OFF)
            await self._error(channel, envelope, off)
            return
        wait = self._hello_wait(channel.key)
        if wait > 0:
            await self._error(
                channel,
                envelope,
                CrdtError(
                    "crdt_busy",
                    "too many hellos on this document; send again shortly",
                    retry_after_ms=max(1, math.ceil(wait * 1000)),
                ),
            )
            return
        since = decode_b64(hello.vv_b64) if hello.vv_b64 else None
        previous = self._held.get(channel.key)
        gate = await self._admit(channel, envelope)
        if gate is None:
            return
        async with gate, AsyncSessionLocal() as db:
            try:
                access = await self.docs.access(
                    db,
                    ref,
                    user=self.host.user,
                    ent=self.host.ent,
                    agent_id=self.host.agent_id,
                    machine_id=self.host.machine_id,
                    rungs=self.host.rungs,
                )
                sync = await self.docs.sync(db, ref, since=since, epoch_seen=hello.epoch_seen)
                writer = access.can_write and grant.can_write
                # A peer is offered back only by a client that still holds the
                # document it wrote with it, in the epoch it is served (it names
                # its vector and that epoch): its counters then continue. A
                # client starting from nothing, or from a new epoch's
                # document, writes as a fresh peer — reusing the old one would
                # write operation ids that peer already spent.
                offered: int | None = None
                if since is not None and hello.epoch_seen == sync.epoch:
                    offered = hello.loro_peer or (
                        previous.loro_peer if previous is not None else None
                    )
                peers: frozenset[int] = frozenset()
                if writer:
                    loro_peer = await self.docs.claim_peer(
                        db, ref, user=self.host.user, offered=offered, holder=self.holder
                    )
                    peers = await self.docs.peers_of(db, ref, user=self.host.user) | {loro_peer}
                elif previous is not None and not previous.writer:
                    loro_peer = previous.loro_peer
                else:
                    # A tab that only reads never writes as its peer: nothing
                    # is stored for it, so reloading cannot grow anything.
                    loro_peer = await self.docs.reader_peer(db)
                await db.commit()
            except CrdtError as exc:
                await db.rollback()
                await self._error(channel, envelope, exc)
                return
        if previous is not None and previous.loro_peer != loro_peer:
            await self.drop(channel.key)
        self._held[channel.key] = _Held(
            ref=ref,
            loro_peer=loro_peer,
            epoch=sync.epoch,
            assembler=ChunkAssembler(
                max_total_bytes=doc_type.limits.max_update_bytes, clock=self.clock
            ),
            peers=peers,
            grant=grant,
            writer=writer,
        )
        limits = CrdtLimits(
            chunk_bytes=CRDT_CHUNK_BYTES,
            max_update_bytes=doc_type.limits.max_update_bytes,
            max_doc_bytes=doc_type.limits.max_doc_bytes,
            max_text_bytes=doc_type.limits.max_text_bytes,
        )
        if len(sync.data) <= CRDT_CHUNK_BYTES:
            pieces: list[CrdtChunk | None] = [None]
        else:
            xfer = f"sync-{loro_peer}-{sync.epoch}-{int(self.clock() * 1000)}"
            pieces = list(split(sync.data, xfer))
        bodies = [
            CrdtSyncPayload(
                mode=cast(CrdtSyncMode, sync.mode),
                vv_b64=encode_b64(sync.vv),
                loro_peer=loro_peer,
                doc_schema=sync.doc_schema,
                limits=limits,
                data_b64=encode_b64(sync.data) if piece is None else None,
                chunk=piece,
                saving=sync.saving,
            )
            for piece in pieces
        ]
        for body in bodies:
            await self.host.send(
                _envelope(
                    channel, epoch=sync.epoch, kind="snapshot", payload=body.model_dump(mode="json")
                )
            )

    async def _admit(
        self, channel: Channel, envelope: DocEnvelope, *, update_id: str | None = None
    ) -> contextlib.AsyncExitStack | None:
        """One of the process's admissions for a request that will hold a
        session while it waits on the sandbox, held until the returned stack
        closes; ``None`` once the peer has been told the lane is busy."""
        stack = contextlib.AsyncExitStack()
        try:
            await stack.enter_async_context(self.docs.admitted())
        except CrdtError as exc:
            await self._error(channel, envelope, exc, update_id=update_id)
            return None
        return stack

    def _hello_wait(self, key: str) -> float:
        """Seconds until ``key`` may take another hello; 0 takes one now."""
        now = self.clock()
        if len(self._hellos) > 64:
            # A bucket that has refilled is the same as none.
            refill = self.hello_burst / self.hello_per_second
            for stale in [k for k, b in self._hellos.items() if now - b.at >= refill]:
                del self._hellos[stale]
        bucket = self._hellos.get(key)
        if bucket is None:
            bucket = self._hellos[key] = _Bucket(tokens=float(self.hello_burst), at=now)
        bucket.tokens = min(
            float(self.hello_burst), bucket.tokens + (now - bucket.at) * self.hello_per_second
        )
        bucket.at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self.hello_per_second

    async def _crdt(self, grant: ChannelGrant, envelope: DocEnvelope) -> None:
        channel = grant.channel
        update_id = envelope.payload.get("update_id")
        named = update_id[:64] if isinstance(update_id, str) else None
        held = self._held.get(channel.key)
        if held is None:
            await self._error(
                channel, envelope, CrdtError("not_synced", "say hello first"), update_id=named
            )
            return
        try:
            payload = parse_crdt_payload(envelope.payload)
        except ValidationError as exc:
            await self._error(
                channel,
                envelope,
                CrdtError("bad_update", str(exc.errors()[0].get("msg", ""))[:200]),
                update_id=named,
            )
            return
        if isinstance(payload, CrdtGonePayload | CrdtSavingPayload):
            await self._error(
                channel,
                envelope,
                CrdtError("unsupported_kind", f"only the server sends {payload.t}"),
            )
            return
        if isinstance(payload, RawCrdtPayload):
            await self._error(
                channel, envelope, CrdtError("unsupported_kind", f"unknown crdt {payload.t!r}")
            )
            return
        if not grant.can_write:
            await self._error(
                channel,
                envelope,
                CrdtError("forbidden", "writing here needs edit rights"),
                update_id=named,
            )
            return
        if not held.writer:
            # Synced as a reader. Granted writing since, it says hello again
            # for a stored peer; still refused by the document's own rules (the
            # share was narrowed, or the socket speaks for an agent), it is told
            # so, or it would say hello and be made a reader again forever.
            refusal = (
                CrdtError("not_synced", "say hello again to write")
                if await self._may_write(held.ref)
                else CrdtError("forbidden", "writing here needs edit rights")
            )
            await self._error(channel, envelope, refusal, update_id=named)
            return
        if isinstance(payload, CrdtEphemeralPayload):
            await self._ephemeral(grant, held, payload)
            return
        await self._update(grant, held, envelope, payload)

    async def _update(
        self, grant: ChannelGrant, held: _Held, envelope: DocEnvelope, payload: CrdtUpdatePayload
    ) -> None:
        channel = grant.channel
        if payload.log_ref is not None:
            await self._error(
                channel,
                envelope,
                CrdtError("bad_update", "a client sends its bytes"),
                update_id=payload.update_id,
            )
            return
        if payload.chunk is not None:
            if payload.chunk.xfer_id != payload.update_id:
                await self._error(
                    channel,
                    envelope,
                    CrdtError("chunk_invalid", "a chunked update's transfer is its update id"),
                    update_id=payload.update_id,
                )
                return
            try:
                whole = held.assembler.add(payload.chunk)
            except ChunkError as exc:
                await self._error(
                    channel, envelope, CrdtError(exc.code, exc.message), update_id=payload.update_id
                )
                return
            if whole is None:
                return
            data = whole
        else:
            data = decode_b64(payload.data_b64 or "")
        gate = await self._admit(channel, envelope, update_id=payload.update_id)
        if gate is None:
            return
        async with gate, AsyncSessionLocal() as db:
            try:
                applied = await self.docs.apply(
                    db,
                    held.ref,
                    user=self.host.user,
                    ent=self.host.ent,
                    agent_id=self.host.agent_id,
                    machine_id=self.host.machine_id,
                    epoch=envelope.epoch,
                    peer=held.loro_peer,
                    update_id=payload.update_id,
                    update=data,
                    socket_peer_id=self.host.peer_id,
                    peers=held.peers,
                )
                await db.commit()
            except CrdtError as exc:
                await db.rollback()
                await self._error(channel, envelope, exc, update_id=payload.update_id)
                if exc.code == "crdt_rejected" and exc.reason == "validator_crash":
                    # Only a death reproduced on the same bytes is a strike. A
                    # timeout answers busy and is never one: slow is the host,
                    # and the edit is still the sender's to send again.
                    await self._count_crash()
                return
        held.epoch = applied.epoch
        await self.host.send(
            _envelope(
                channel,
                epoch=applied.epoch,
                kind="ack",
                payload=CrdtAckPayload(
                    update_id=payload.update_id,
                    changed=applied.changed,
                    vv_b64=encode_b64(applied.vv),
                ).model_dump(mode="json"),
            )
        )
        # The writer is answered, then everyone else; moving this replica's
        # worker onto the commit can wait for both.
        await self.docs.broadcast(held.ref, applied)
        await self.docs.after_commit(held.ref, applied)

    async def _may_write(self, ref: DocRef) -> bool:
        """Whether this socket's person may write ``ref`` now, by the
        document's own rules."""
        async with AsyncSessionLocal() as db:
            try:
                access = await self.docs.access(
                    db,
                    ref,
                    user=self.host.user,
                    ent=self.host.ent,
                    agent_id=self.host.agent_id,
                    machine_id=self.host.machine_id,
                    rungs=self.host.rungs,
                )
            except CrdtError:
                return False
        return access.can_write

    async def _count_crash(self) -> None:
        now = self.clock()
        self._crashes.append(now)
        while self._crashes and self._crashes[0] <= now - VALIDATOR_CRASH_WINDOW_SECONDS:
            self._crashes.popleft()
        if len(self._crashes) >= MAX_VALIDATOR_CRASHES:
            log.error("crdt.socket.closed_for_crashes", user_id=str(self.host.user.id))
            await self.host.close(
                close_codes.TOO_MANY, "too many updates the server could not read"
            )

    async def _ephemeral(
        self, grant: ChannelGrant, held: _Held, payload: CrdtEphemeralPayload
    ) -> None:
        channel = grant.channel
        try:
            relayed, facts = await self.docs.caret(
                held.ref, peer=held.loro_peer, data=decode_b64(payload.data_b64)
            )
        except CrdtError as exc:
            await self.host.send(
                _envelope(
                    channel,
                    epoch=held.epoch,
                    kind="error",
                    # Named as a caret's, so the tab drops the caret and
                    # never mistakes the refusal for its update's or hello's.
                    payload=ErrorPayload(
                        code=exc.code, message=exc.message[:500], reason="ephemeral"
                    ).model_dump(mode="json"),
                )
            )
            return
        stamped = CrdtEphemeralPayload(
            data_b64=encode_b64(relayed),
            loro_peer=held.loro_peer,
            user_id=str(self.host.user.id),
            display_name=self.host.display_name(),
            email=(self.host.user.email or "")[:320],
        )
        envelope = DocEnvelope(
            doc_id=channel.doc_id,
            doc_type=channel.doc_type,
            epoch=held.epoch,
            peer_id=self.host.peer_id,
            seq=0,
            kind="crdt",
            payload=stamped.model_dump(mode="json"),
        )
        try:
            async with AsyncSessionLocal() as db:
                # A caret is gone the moment it is superseded: its NOTIFY
                # need not wait for the WAL, so its turn at the database's
                # notify lock stays short.
                await db.execute(text("SET LOCAL synchronous_commit = off"))
                await presence.publish_ephemeral(
                    db,
                    presence.ephemeral_event(
                        grant=grant,
                        type=CRDT_EPHEMERAL_EVENT_TYPE,
                        body={
                            "envelope": envelope.model_dump(mode="json"),
                            # A notebook caret names its cell, for the
                            # notices an agent's edit there is answered with.
                            **(
                                {CARET_CELL_KEY: facts["cell"]}
                                if isinstance(facts.get("cell"), str)
                                else {}
                            ),
                        },
                    ),
                )
                await db.commit()
        except presence.EphemeralTooLargeError as exc:
            log.warning("crdt.ephemeral.too_large", error=safe_error(exc))

    # -- outbound ------------------------------------------------------------

    def wants(self, event: HubEvent) -> bool:
        """Whether ``event`` is CRDT traffic this socket turns into frames."""
        if event.type in (CRDT_EPHEMERAL_EVENT_TYPE, CRDT_UPDATE_EVENT_TYPE):
            return True
        raw = event.payload.get("envelope")
        return isinstance(raw, dict) and self.serves(raw.get("doc_type"))

    def has_source(self, doc_type: object) -> bool:
        """Whether ``doc_type`` is served and rests in a source (a file): the
        documents whose writing can be taken away by the source itself."""
        doc = self.docs.registry.get(doc_type) if isinstance(doc_type, str) else None
        return doc is not None and doc.source is not None

    @staticmethod
    def saving_notice(event: HubEvent) -> str | None:
        """The channel a saving notice (:class:`CrdtSavingPayload`) is about,
        or ``None`` for any other event."""
        raw = event.payload.get("envelope")
        if not isinstance(raw, dict) or raw.get("kind") != "crdt":
            return None
        payload = raw.get("payload")
        if not isinstance(payload, dict) or payload.get("t") != "saving":
            return None
        return event.channel

    def serves(self, doc_type: object) -> bool:
        """Whether ``doc_type`` is a document type this lane serves: every
        type in the store's registry, and nothing else."""
        return isinstance(doc_type, str) and doc_type in self.docs.registry.names()

    async def outbound(self, event: HubEvent) -> list[Any]:
        """The frames ``event`` becomes for this socket (none for its own);
        none yet for a channel whose hello is being answered, which sends
        them after its answer (:meth:`_hello`)."""
        raw = event.payload.get("envelope")
        if isinstance(raw, dict) and self._answering:
            with contextlib.suppress(ValidationError):
                envelope = DocEnvelope.model_validate(raw)
                key = Channel(doc_type=envelope.doc_type, doc_id=envelope.doc_id).key
                held_back = self._answering.get(key)
                if held_back is not None:
                    held_back.append(event)
                    return []
        return await self._frames(event)

    async def _frames(self, event: HubEvent) -> list[Any]:
        raw = event.payload.get("envelope")
        if isinstance(raw, dict) and raw.get("kind") == "reload":
            # Whatever moved the document (the lane switched off, a new epoch,
            # a quarantine), this socket's hold on it ends here: a caret or an
            # update now needs the hello that re-checks the lane and access.
            with contextlib.suppress(ValidationError):
                envelope = DocEnvelope.model_validate(raw)
                await self.drop(Channel(doc_type=envelope.doc_type, doc_id=envelope.doc_id).key)
        return await relay_frames(self.docs, self.host.org_id, event, own_peer=self.host.peer_id)


async def relay_frames(
    docs: CrdtDocs, org_id: UUID, event: HubEvent, *, own_peer: str
) -> list[Any]:
    """The frames a CRDT announcement becomes for a socket that holds its
    channel: an update announced by reference read back from the log, and
    anything over one frame chunked; nothing for the socket's own update
    (``own_peer`` sent it and was answered with an ack). Shared by a person's
    lane and a box following a document it holds."""
    raw = event.payload.get("envelope")
    try:
        envelope = DocEnvelope.model_validate(raw)
    except ValidationError:
        log.warning("crdt.outbound.bad_envelope", channel=event.channel)
        return []
    if envelope.kind != "crdt":
        return [DocFrame(envelope=envelope)]
    if envelope.peer_id == own_peer:
        # Its own update was acknowledged; its own caret is its own.
        return []
    try:
        payload = parse_crdt_payload(envelope.payload)
    except ValidationError:
        log.warning("crdt.outbound.bad_payload", channel=event.channel)
        return []
    if not isinstance(payload, CrdtUpdatePayload):
        return [DocFrame(envelope=envelope)]
    if payload.log_ref is not None:
        ref = DocRef(org_id=org_id, doc_type=envelope.doc_type, doc_id=envelope.doc_id)
        async with AsyncSessionLocal() as db:
            data = await docs.logged_delta(db, ref, epoch=envelope.epoch, log_seq=payload.log_ref)
        if data is None:
            # Folded into a snapshot before it reached us: the client
            # resyncs from its vector and loses nothing.
            channel = Channel(doc_type=envelope.doc_type, doc_id=envelope.doc_id)
            return [
                _envelope(
                    channel,
                    epoch=envelope.epoch,
                    kind="error",
                    payload=ErrorPayload(code="crdt_resync", message="resync").model_dump(
                        mode="json"
                    ),
                )
            ]
    else:
        data = decode_b64(payload.data_b64 or "")
    whole: list[CrdtChunk | None] = (
        [None] if len(data) <= CRDT_CHUNK_BYTES else list(split(data, payload.update_id))
    )
    bodies = [
        payload.model_copy(
            update={
                "data_b64": encode_b64(data) if piece is None else None,
                "chunk": piece,
                "log_ref": None,
            }
        )
        for piece in whole
    ]
    return [
        DocFrame(
            envelope=envelope.model_copy(
                update={"payload": body.model_dump(mode="json", exclude_none=True)}
            )
        )
        for body in bodies
    ]


def _refusal_for(exc: Exception, *, channel: str, kind: str) -> CrdtError:
    """What a frame that failed outside the lane's own refusals is answered.

    A busy database (a held row past ``lock_timeout``, a drained pool, a
    database restarting) is ``crdt_busy`` with the wait the classifier names:
    nothing was written, and the same frame sent again later is taken. Only
    what is not contention is ``internal``. One mapping for every frame kind,
    so no path (a hello, an update, a replayed one) answers a lock timeout as
    if the frame were broken."""
    busy = busy_for(exc)
    if busy is not None:
        log.warning("crdt.socket.frame_busy", channel=channel, kind=kind, code=busy.reason)
        return busy
    # Not ``log.exception``: the traceback ends in the error's text, which for
    # a database error is the statement's parameters.
    log.error("crdt.socket.frame_failed", channel=channel, kind=kind, error=safe_error(exc))
    return CrdtError("internal", f"{type(exc).__name__}", retry_after_ms=500)


__all__ = [
    "CRDT_EPHEMERAL_EVENT_TYPE",
    "HELLO_BURST",
    "HELLO_PER_SECOND",
    "MAX_VALIDATOR_CRASHES",
    "VALIDATOR_CRASH_WINDOW_SECONDS",
    "CrdtHost",
    "CrdtSocket",
    "relay_frames",
]
