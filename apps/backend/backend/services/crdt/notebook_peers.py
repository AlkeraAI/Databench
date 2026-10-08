"""Notebook peers: writers of a ``notebook`` document that speak operations.

An agent (through its chat's tools) and a box (the engine running beside the
kernel) never hold a Loro document. They send the engine's document
operations (insert, edit, replace, delete, restore, move, rename, set_kind,
set_config, set_setting) with the token of the state they read, and the
sandbox applies them as a freshly minted Loro peer on a fork at exactly that
state, so they are concurrent with everything people typed since and Loro
merges the two (``backend.services.crdt.sandbox.notebook.apply_ops``). A batch
is atomic: one refused operation refuses it whole (:class:`NotebookOpError`).

A batch with a ``submit_id`` is idempotent: a retry is answered from this
replica's memory, or, on another replica, recognised in the log and answered
with the document as it is now (``repeat``), never applied twice.

Who may be a notebook peer is decided by the caller (the notebook routes: the
Files policy, then the lease fence). The update is written with the caller's
``agent_id`` and person (``author``) as given.

This module also runs the server's own peer: normalization (written as
``server:normalize``) after a person's update left the document out of normal
form, and the reads the notebook routes need (the view at a frontier, the
dependency graph's diagnostics).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from alkera_core.logging import get_logger
from alkera_core.models import CrdtDoc, User
from alkera_core.notebooks.models import NotebookEpochTail, NotebookPeer
from alkera_core.schemas.realtime import SERVER_PEER_ID
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.errors import CrdtError
from backend.services.crdt.notebook_type import NORMALIZER_AGENT_ID
from backend.services.crdt.registry import Access, DocRef
from backend.services.crdt.sandbox.pool import SandboxRefusedError
from backend.services.crdt.sandbox.protocol import Frame, decode_projection
from backend.services.crdt.sessions import OPS_UPDATE_PREFIX
from backend.services.crdt.switch import LIVE_EDITING_OFF, OFF_MESSAGE, covers
from backend.services.crdt.text_peers import TextPeers, decode_token, encode_token

if TYPE_CHECKING:
    from backend.services.crdt.docs import CrdtDocs

log = get_logger(__name__)

#: The update id a batch of operations is logged under (the session engine
#: reads it to tell the folder holder's own batches apart).
UPDATE_ID_PREFIX: Final = OPS_UPDATE_PREFIX
#: How many batch answers a replica keeps for a retry to be answered from.
ANSWERS_KEPT: Final = 256
#: How often a view waiting for a frontier looks again.
FRONTIER_POLL_SECONDS: Final = 0.02
#: How long after a commit the dependency graph is analysed: a burst of
#: typing is analysed once, at its end.
GRAPH_DEBOUNCE_SECONDS: Final = 0.3
#: How many notebooks' latest graphs a replica keeps.
GRAPHS_KEPT: Final = 256


class NotebookOpError(CrdtError):
    """An operation the batch was refused for: its index in the batch and one
    of the operation error codes (``cell_not_found``, ``edit_not_found``,
    ``edit_ambiguous``, ``invalid_name``, ``unknown_kind``, ``invalid_config``,
    ``setup_must_be_first``, ``cap_exceeded``)."""

    def __init__(self, index: int, code: str, message: str) -> None:
        super().__init__("op_refused", message, reason=code)
        self.index = index
        self.op_code = code
        self.op_message = message


@dataclass(frozen=True, slots=True)
class NotebookApplied:
    """What a batch did. ``cells`` are the touched cells as they stand after
    it (the ``CellAfterOp`` shape); ``notices`` what the sandbox can tell
    (``edited_deleted_cell``, ``kind_changed_by_other``); ``caret`` the
    ephemeral update that puts the writer's caret at the end of its last edit
    for ten seconds, ready to relay on the document's channel."""

    token: str
    repeat: bool
    cells: list[dict[str, Any]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    caret: bytes | None = None
    peer: int = 0


@dataclass(frozen=True, slots=True)
class NotebookDocView:
    """The live document at the worker's position: its token, whether it
    covers the frontier asked about, and the view (``format``, ``settings``,
    ``cells`` in order with their sources)."""

    token: str
    covered: bool
    view: dict[str, Any]


def _frontier(token: str | None, epoch: int) -> tuple[bytes | None, bool | None]:
    """The version vector a frontier names in this epoch, or the verdict when
    it names another.

    A frontier is opaque to everyone but this store. It is either a token
    this store handed out (``<epoch>.<vector>``) or what an editor holds: the
    base64 of its Loro document's encoded version vector, read as a vector of
    the current epoch (one from before a history restart names operations
    the new epoch never has, so it is simply not covered). A token from an
    earlier epoch is covered (a new epoch starts from all of the old one's
    content), one from a later epoch is not (this replica has not seen it
    yet), and one that does not parse is treated as no frontier."""
    if token and "." not in token:
        try:
            raw = base64.b64decode(
                token.replace("-", "+").replace("_", "/") + "=" * (-len(token) % 4), validate=True
            )
        except (binascii.Error, ValueError):
            return None, None
        return (raw or None), None
    named = decode_token(token)
    if named is None:
        return None, None
    named_epoch, vv = named
    if named_epoch == epoch:
        return vv, None
    return None, named_epoch < epoch


@dataclass
class NotebookPeers:
    """See the module docstring."""

    docs: CrdtDocs
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    graph_debounce: float = GRAPH_DEBOUNCE_SECONDS
    _answers: OrderedDict[str, NotebookApplied] = field(default_factory=OrderedDict, init=False)
    #: Each notebook's latest analysed graph, with the token it is of.
    _graphs: OrderedDict[str, tuple[str, dict[str, Any]]] = field(
        default_factory=OrderedDict, init=False
    )
    _graph_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _graph_again: set[str] = field(default_factory=set, init=False)

    async def _open(self, db: AsyncSession, ref: DocRef) -> CrdtDoc:
        """The document row, locked, its session live: created from the file
        when nobody has it open, opened again from the file when a session
        was closed (raises ``not_editable`` when the file is not editable)."""
        doc = await self.docs.ensure(db, ref)
        if doc.source_etag is None and await self.docs.sessions.reopen(db, ref):
            reopened = await self.docs.locked(db, ref)
            if reopened is None:  # pragma: no cover - reopened just now, under the lock
                raise CrdtError("not_found")
            return reopened
        if doc.source_etag is None:
            raise CrdtError("not_editable", "this notebook cannot be edited live")
        return doc

    async def _readable(self, db: AsyncSession, ref: DocRef) -> CrdtDoc:
        """The document row for a read: as committed and unlocked when a
        session is live (a view, a run's wait for its frontier, the graph
        never queue behind people typing), opened under the lock only when it
        must be created or opened again."""
        doc = await self.docs.row(db, ref)
        if doc is not None and doc.source_etag is not None:
            return doc
        return await self._open(db, ref)

    async def apply(
        self,
        ref: DocRef,
        *,
        ops: list[dict[str, Any]],
        base_token: str | None,
        submit_id: str | None,
        agent_id: str | None,
        author: User | None,
        actor_key: str | None = None,
    ) -> NotebookApplied:
        """Apply ``ops`` at ``base_token`` (see the module docstring), as the
        Loro peer ``actor_key`` (an agent session, a box machine, a person)
        holds on this document, claimed on its first batch of the epoch; a
        fresh peer per batch when no writer is named. Raises
        :class:`NotebookOpError` for a refused operation and :class:`CrdtError`
        when the document cannot take the batch."""
        memory_key = f"{ref.key}:{submit_id}" if submit_id else None
        if memory_key is not None and memory_key in self._answers:
            return self._answers[memory_key]
        await self._refuse_while_off(ref)
        update_id = f"{UPDATE_ID_PREFIX}{submit_id}" if submit_id else None
        doc_type = self.docs.type_of(ref)
        async with self.docs.session_factory() as db, self.docs.admitted():
            doc = await self._open(db, ref)
            if update_id is not None and await TextPeers._logged(db, ref, doc, update_id):
                token = encode_token(doc.epoch, bytes(doc.vv))
                await db.commit()
                return NotebookApplied(token=token, repeat=True)
            named = decode_token(base_token)
            tail = (
                await self._tail_of(db, ref, named[0], doc.epoch)
                if named is not None and named[0] < doc.epoch
                else None
            )
            reply = None
            if tail is not None and named is not None:
                # Made on an epoch the history restarted from: applied there
                # and carried over cell by cell, by a peer minted for it (the
                # old epoch's copy exists only in the sandbox call).
                peer = await self.docs.seed_peer(db)
                try:
                    reply = await self._rebase(db, ref, doc, peer, ops, tail, named[1])
                except SandboxRefusedError as exc:
                    if exc.code != "bad_base":
                        raise CrdtError("crdt_rejected", exc.message, reason=exc.code) from exc
            if reply is None:
                peer = await self._peer_of(db, ref, doc, actor_key)
                base, _ = _frontier(base_token, doc.epoch)
                try:
                    reply = await self._ops(db, ref, doc, peer, ops, base)
                except SandboxRefusedError as exc:
                    if exc.code != "bad_base":
                        raise CrdtError("crdt_rejected", exc.message, reason=exc.code) from exc
                    # A token naming a version this document never had: the
                    # operations are matched at the head instead.
                    reply = await self._ops(db, ref, doc, peer, ops, None)
            outcome = reply.header.get("outcome")
            if outcome == "op_error":
                await db.rollback()
                raise NotebookOpError(
                    int(reply.header.get("op_index") or 0),
                    str(reply.header.get("code") or "invalid_config"),
                    str(reply.header.get("message") or ""),
                )
            delta, vv, projection, raw_result = reply.blobs
            result = decode_projection(raw_result)
            caret_hex = result.get("caret")
            caret = bytes.fromhex(caret_hex) if isinstance(caret_hex, str) else None
            if outcome == "dup":
                answer = NotebookApplied(
                    token=encode_token(doc.epoch, bytes(doc.vv)),
                    repeat=False,
                    cells=list(result.get("cells") or []),
                    created=[],
                    touched=[],
                    notices=list(result.get("notices") or []),
                    caret=caret,
                    peer=peer,
                )
                await db.commit()
                return self._keep(memory_key, answer)
            if outcome != "ok":
                await db.rollback()
                raise CrdtError(
                    "crdt_rejected",
                    "the operations break this document's rules",
                    reason=str(reply.header.get("reason") or "rejected"),
                )
            if doc.snapshot_bytes + doc.log_bytes + len(delta) > doc_type.limits.max_doc_bytes:
                await db.rollback()
                self.docs._schedule(ref, "rotate", reason="doc_full")
                raise CrdtError("doc_full", "the document's history is full; it is being restarted")
            applied = await self.docs.append(
                db,
                ref,
                doc,
                delta=delta,
                vv=vv,
                projection=decode_projection(projection),
                peer=peer,
                update_id=update_id or f"{UPDATE_ID_PREFIX}{peer}-{doc.log_seq + 1}",
                author=author,
                agent_id=agent_id,
                socket_peer_id=SERVER_PEER_ID,
                access=Access(
                    can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)
                ),
            )
            await db.commit()
        await self.docs.broadcast(ref, applied)
        await self.docs.after_commit(ref, applied)
        log.info("crdt.notebook.ops_applied", doc=ref.key, ops=len(ops), agent_id=agent_id)
        return self._keep(
            memory_key,
            NotebookApplied(
                token=encode_token(applied.epoch, applied.vv),
                repeat=False,
                cells=list(result.get("cells") or []),
                created=[str(c) for c in result.get("created") or []],
                touched=[str(c) for c in result.get("touched") or []],
                notices=list(result.get("notices") or []),
                caret=caret,
                peer=peer,
            ),
        )

    async def _refuse_while_off(self, ref: DocRef) -> None:
        """Refuse a write while live editing is off for the notebook's org, as
        the socket refuses an editor (``crdt_unsupported``, reason
        ``live_editing_off``): a document nobody may open must not take an
        agent's or a box's batch either."""
        if covers(self.docs.type_of(ref)) and not await self.docs.switch.on(ref.org_id):
            raise CrdtError("crdt_unsupported", OFF_MESSAGE, reason=LIVE_EDITING_OFF)

    async def rebase_update(self, ref: DocRef, *, epoch: int, update: bytes, author: User) -> str:
        """Carry an editor's update written in ``epoch`` that never reached
        it (the history restarted first) into the current epoch, cell by cell
        (the sandbox's ``rebase_update``), as ``author``. Answers the token
        of the state after it. Raises ``crdt_rebase_unavailable`` when the
        state ``epoch`` ended in is not kept (the editor reloads, and what it
        had not sent is lost), ``crdt_rejected`` for an update the document's
        rules refuse, and ``crdt_resync`` for one that depends on history the
        old epoch never held."""
        await self._refuse_while_off(ref)
        doc_type = self.docs.type_of(ref)
        async with self.docs.session_factory() as db, self.docs.admitted():
            doc = await self._open(db, ref)
            current, head = doc.epoch, bytes(doc.vv)
            tail = await self._tail_of(db, ref, epoch, current) if epoch < current else None
            if tail is None:
                await db.rollback()
                raise CrdtError(
                    "crdt_rebase_unavailable",
                    "the state that update was written on is no longer kept",
                    epoch=current,
                )
            peer = await self.docs.seed_peer(db)
            reply = await self.docs.at_position(
                db,
                ref,
                doc,
                {
                    "op": "nb_rebase_update",
                    "key": self.docs.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                    "rules": doc_type.rules,
                    "peer": peer,
                },
                [bytes(tail.snapshot), update, bytes(tail.next_base_vv)],
                maintenance=True,
            )
            outcome = reply.header.get("outcome")
            delta, vv, projection, raw = reply.blobs
            found = decode_projection(raw)
            if outcome in ("dup", "resync", "reject"):
                await db.rollback()
                if outcome == "dup":
                    return encode_token(current, head)
                if outcome == "resync":
                    raise CrdtError("crdt_resync", "the update depends on history the server lacks")
                raise CrdtError(
                    "crdt_rejected",
                    "the update breaks this document's rules",
                    reason=str(found.get("reason") or "rejected"),
                )
            applied = await self.docs.append(
                db,
                ref,
                doc,
                delta=delta,
                vv=vv,
                projection=decode_projection(projection),
                peer=peer,
                update_id=f"rebase-{peer}",
                author=author,
                agent_id=None,
                socket_peer_id=SERVER_PEER_ID,
                access=Access(
                    can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)
                ),
            )
            touched = found.get("touched")
            on_write = getattr(doc_type, "on_write", None)
            if on_write is not None and isinstance(touched, list) and touched:
                await on_write(
                    db, ref=ref, author=author, agent_id=None, notes={"touched": touched}
                )
            await db.commit()
        await self.docs.broadcast(ref, applied)
        await self.docs.after_commit(ref, applied)
        log.info("crdt.notebook.rebased_update", doc=ref.key, epoch=epoch)
        return encode_token(applied.epoch, applied.vv)

    async def _peer_of(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, actor_key: str | None
    ) -> int:
        """The peer ``actor_key`` writes this document's epoch as: the one it
        claimed, or a fresh one it claims now (under the document's row lock,
        which the caller holds, so two batches of one writer never race)."""
        if actor_key is None:
            return await self.docs.seed_peer(db)
        item_id = UUID(ref.doc_id)
        held = (
            await db.execute(
                select(NotebookPeer)
                .where(
                    NotebookPeer.org_id == ref.org_id,
                    NotebookPeer.item_id == item_id,
                    NotebookPeer.actor_key == actor_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if held is not None and held.epoch == doc.epoch:
            return int(held.loro_peer)
        peer = await self.docs.seed_peer(db)
        await db.execute(
            pg_insert(NotebookPeer)
            .values(
                org_id=ref.org_id,
                item_id=item_id,
                actor_key=actor_key,
                epoch=doc.epoch,
                loro_peer=peer,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "item_id", "actor_key"],
                set_={"epoch": doc.epoch, "loro_peer": peer, "claimed_at": func.now()},
            )
        )
        return peer

    @staticmethod
    async def _tail_of(
        db: AsyncSession, ref: DocRef, epoch: int, current: int
    ) -> NotebookEpochTail | None:
        """The state epoch ``epoch`` ended in, when the history restarted from
        it straight into the current epoch."""
        tail = await db.get(NotebookEpochTail, (ref.org_id, UUID(ref.doc_id), epoch))
        return tail if tail is not None and tail.next_epoch == current else None

    async def _rebase(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        peer: int,
        ops: list[dict[str, Any]],
        tail: NotebookEpochTail,
        base: bytes,
    ) -> Frame:
        return await self.docs.at_position(
            db,
            ref,
            doc,
            {
                "op": "nb_rebase_ops",
                "key": self.docs.cache_key(ref, doc),
                "epoch": doc.epoch,
                "log_seq": doc.log_seq,
                "rules": self.docs.type_of(ref).rules,
                "peer": peer,
            },
            [
                json.dumps(ops, separators=(",", ":")).encode("utf-8"),
                bytes(tail.snapshot),
                base,
                bytes(tail.next_base_vv),
            ],
            maintenance=True,
        )

    async def _ops(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        peer: int,
        ops: list[dict[str, Any]],
        base: bytes | None,
    ) -> Frame:
        blobs = [json.dumps(ops, separators=(",", ":")).encode("utf-8")]
        if base is not None:
            blobs.append(base)
        return await self.docs.at_position(
            db,
            ref,
            doc,
            {
                "op": "nb_ops",
                "key": self.docs.cache_key(ref, doc),
                "epoch": doc.epoch,
                "log_seq": doc.log_seq,
                "rules": self.docs.type_of(ref).rules,
                "peer": peer,
            },
            blobs,
            maintenance=True,
        )

    def _keep(self, memory_key: str | None, answer: NotebookApplied) -> NotebookApplied:
        if memory_key is None:
            return answer
        self._answers[memory_key] = answer
        self._answers.move_to_end(memory_key)
        while len(self._answers) > ANSWERS_KEPT:
            self._answers.popitem(last=False)
        return answer

    async def view(
        self, ref: DocRef, *, frontier: str | None = None, wait_seconds: float = 0.0
    ) -> NotebookDocView:
        """The live document, waiting up to ``wait_seconds`` for it to cover
        ``frontier`` (a token a client holds: its typing that may not have
        reached this replica yet). ``covered`` says whether it did."""
        deadline = self.clock() + max(0.0, wait_seconds)
        while True:
            found = await self._view_once(ref, frontier)
            if found.covered or self.clock() >= deadline:
                return found
            await self.sleep(FRONTIER_POLL_SECONDS)

    async def _view_once(self, ref: DocRef, frontier: str | None) -> NotebookDocView:
        async with self.docs.session_factory() as db, self.docs.admitted():
            doc = await self._readable(db, ref)
            await db.commit()
            vv, decided = _frontier(frontier, doc.epoch)
            reply = await self.docs.at_position(
                db,
                ref,
                doc,
                {
                    "op": "nb_view",
                    "key": self.docs.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                    "rules": self.docs.type_of(ref).rules,
                },
                [] if vv is None else [vv],
            )
            await db.commit()
        raw, at = reply.blobs
        view = decode_projection(raw)
        covered = bool(view.pop("covered", True)) if decided is None else decided
        return NotebookDocView(token=encode_token(doc.epoch, at), covered=covered, view=view)

    async def graph(self, ref: DocRef) -> tuple[str, dict[str, Any]]:
        """The dependency graph's diagnostics (the format API's ``analyze``)
        over the live document, and the token of the state analysed."""
        async with self.docs.session_factory() as db, self.docs.admitted():
            doc = await self._readable(db, ref)
            await db.commit()
            reply = await self.docs.at_position(
                db,
                ref,
                doc,
                {
                    "op": "nb_graph",
                    "key": self.docs.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                    "rules": self.docs.type_of(ref).rules,
                },
                [],
                maintenance=True,
            )
            await db.commit()
        raw, vv = reply.blobs
        return encode_token(doc.epoch, vv), decode_projection(raw)

    def holder_wrote(self, ref: DocRef) -> None:
        """The machine holding the notebook's folder applied a batch: it is
        told the document moved (:meth:`TextPeers.holder_wrote`), so it writes
        the notebook's file."""
        self.docs.peers.holder_wrote(ref)

    def changed(self, ref: DocRef) -> None:
        """An update committed: analyse the graph once the document has been
        still for :attr:`graph_debounce` (off the hot path: nothing waits on
        it). A commit while an analysis runs asks for one more after it."""
        running = self._graph_tasks.get(ref.key)
        if running is not None and not running.done():
            self._graph_again.add(ref.key)
            return
        task = asyncio.create_task(self._graph_later(ref))
        self._graph_tasks[ref.key] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._graph_tasks.get(ref.key) is done:
                self._graph_tasks.pop(ref.key, None)
            if ref.key in self._graph_again:
                self._graph_again.discard(ref.key)
                self.changed(ref)

        task.add_done_callback(finished)

    async def _graph_later(self, ref: DocRef) -> None:
        await self.sleep(self.graph_debounce)
        try:
            token, graph = await self.graph(ref)
        except Exception as exc:  # diagnostics are best effort; the next commit retries
            log.info("crdt.notebook.graph_failed", doc=ref.key, error=type(exc).__name__)
            return
        self._graphs[ref.key] = (token, graph)
        self._graphs.move_to_end(ref.key)
        while len(self._graphs) > GRAPHS_KEPT:
            self._graphs.popitem(last=False)

    def graph_at(self, ref: DocRef, token: str) -> dict[str, Any] | None:
        """The format API's analysis of the document at ``token`` (``{cells:
        {id: {defs, refs, errors}}, edges}``), when this replica has analysed
        exactly that state; ``None`` otherwise."""
        found = self._graphs.get(ref.key)
        if found is None or found[0] != token:
            return None
        return dict(found[1])

    async def drain_graphs(self) -> None:
        """Wait for every analysis scheduled so far (tests, shutdown)."""
        while self._graph_tasks:
            await asyncio.gather(*list(self._graph_tasks.values()), return_exceptions=True)

    async def normalize(self, ref: DocRef) -> bool:
        """Bring the document back to normal form as the server peer; whether
        it wrote anything."""
        doc_type = self.docs.type_of(ref)
        async with self.docs.session_factory() as db, self.docs.admitted():
            doc = await self.docs.locked(db, ref)
            if doc is None:
                return False
            peer = await self.docs.seed_peer(db)
            reply = await self.docs.at_position(
                db,
                ref,
                doc,
                {
                    "op": "nb_normalize",
                    "key": self.docs.cache_key(ref, doc),
                    "epoch": doc.epoch,
                    "log_seq": doc.log_seq,
                    "rules": doc_type.rules,
                    "peer": peer,
                },
                [],
                maintenance=True,
            )
            if reply.header.get("outcome") != "ok":
                await db.rollback()
                return False
            delta, vv, projection, _ = reply.blobs
            applied = await self.docs.append(
                db,
                ref,
                doc,
                delta=delta,
                vv=vv,
                projection=decode_projection(projection),
                peer=peer,
                update_id=f"normalize-{peer}",
                author=None,
                agent_id=NORMALIZER_AGENT_ID,
                socket_peer_id=SERVER_PEER_ID,
                access=Access(
                    can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)
                ),
            )
            await db.commit()
        await self.docs.broadcast(ref, applied)
        await self.docs.after_commit(ref, applied)
        log.info("crdt.notebook.normalized", doc=ref.key)
        return True


__all__ = [
    "ANSWERS_KEPT",
    "GRAPH_DEBOUNCE_SECONDS",
    "UPDATE_ID_PREFIX",
    "NotebookApplied",
    "NotebookDocView",
    "NotebookOpError",
    "NotebookPeers",
]
