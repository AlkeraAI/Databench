"""Text peers: writers of a co-edited document that speak whole texts, not Loro.

The machine holding a file's folder (a chat's box) is where an agent edits the
file, on disk. While people have the file open live, the box joins its
document as a text peer instead of uploading what the agent wrote and leaving
the session to merge it from the drive:

* **read** answers the document's content and a version token naming the state
  it was taken at (``"<epoch>.<base64 vector>"``, opaque to the box);
* **submit** takes a whole text and the token of the state the box last wrote
  to its disk. The agent's change is the difference between the two, applied
  by a server-minted Loro peer (one per document on a replica, reused while
  the base holds all it wrote) on a fork at exactly that state, so it is
  concurrent with every edit people made since and Loro merges the two. The
  answer is the merged content, its token, and the token of the state the
  submitted text went into (what the box holds if its disk moved again while
  the answer was on its way), with that state's content when it is not
  exactly the text sent. A token from an earlier epoch falls
  back to the version the box last agreed with the drive (``base_etag``) and,
  failing that, to the closest version the session remembers, merged adding
  what the text adds and removing nothing;
* **changed** is called after every committed update: once the document has
  been still for :attr:`TextPeers.notify_delay`, the holder's machine is told
  on its channel (a ``machine.request`` of kind ``live_text``, carrying the
  token and never the text) so it reads the document and writes it to disk.

A submit is idempotent by its ``submit_id`` (within its document): a retry
after a lost answer is answered from memory or, on another replica,
recognised in the log and answered with the document as it is now
(``repeat``), never applied twice.

Every answer says whether the document is saving (``saved``): ``False`` when
its write back is parked, so the drive does not hold the text and will not
get it without a person. The box then uploads the file the ordinary way,
which the session recognises as the content it already holds.

Who may be a text peer is decided by the caller (the Files route): the Files
policy, then the fence of the lease covering the file, which only its holder
passes. The update is written as the holder's machine (``agent_id``), with no
person as its author; when no person can be named for the write back either,
it is written to the drive as that machine (:mod:`.sessions`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Final, TypeVar

from alkera_core.events import HubEvent
from alkera_core.events.listener import EPHEMERAL_CHANNEL, ephemeral_payload
from alkera_core.files.promotion import machine_event
from alkera_core.logging import get_logger
from alkera_core.models import CrdtDoc, CrdtUpdate
from alkera_core.models.crdt_doc import PeerView
from alkera_core.schemas.realtime import SERVER_PEER_ID, LiveTextRequest, decode_b64, encode_b64
from sqlalchemy import cast, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.errors import safe_error
from backend.services.crdt.registry import Access, DocRef, SourceHolder
from backend.services.crdt.sandbox.pool import SandboxRefusedError
from backend.services.crdt.sandbox.protocol import Frame, decode_projection
from backend.services.crdt.sessions import (
    TEXT_PEER_UPDATE_PREFIX,
    changed_between,
    named_version,
)
from backend.services.infra import now as _now

if TYPE_CHECKING:
    from backend.services.crdt.docs import Applied, CrdtDocs

log = get_logger(__name__)

#: How many documents' merge peers a replica remembers.
MERGE_PEERS_KEPT: Final = 4096
#: How long a document must be still before its holder is told it moved. A
#: burst of typing is one notice, sent this long after it began.
NOTIFY_DELAY_SECONDS: Final = 0.25
#: How long the holder of a document's folder is remembered between notices.
HOLDER_CACHE_SECONDS: Final = 5.0
#: The most documents a replica remembers a holder, or an open notice, for:
#: entries past their use are dropped first, then the oldest.
REMEMBERED_DOCS: Final = 4096
#: How many submit answers a replica keeps for a retry to be answered from.
ANSWERS_KEPT: Final = 256
#: The most text those answers hold together: a file is up to 2 MiB, and a
#: retry the memory no longer answers is still recognised in the log.
ANSWER_BYTES_KEPT: Final = 8 * 1024 * 1024
#: The update id a text peer's submit is logged under.
UPDATE_ID_PREFIX: Final = TEXT_PEER_UPDATE_PREFIX
#: How long a text peer's read or write counts it as one: only a holder that
#: joined lately is told the document moved (a box from before text peers
#: knows no such notice and would log every one).
TEXT_PEER_SECONDS: Final = 120.0
#: How stale that mark may get before a read moves it again.
TEXT_PEER_REMARK_SECONDS: Final = 30.0
#: How often a document being opened may tell a holder that has not joined
#: (a box from before text peers logs one line for it).
OPEN_NOTICE_SECONDS: Final = 60.0
#: How many states a text peer was handed the document remembers, newest
#: first: candidates for what a box that lost its token has on disk.
VIEWS_KEPT: Final = 24


def encode_token(epoch: int, vv: bytes) -> str:
    """The token naming the version ``vv`` of epoch ``epoch``."""
    return f"{epoch}.{base64.urlsafe_b64encode(vv).decode('ascii').rstrip('=')}"


def decode_token(token: str | None) -> tuple[int, bytes] | None:
    """The epoch and version a token names, or ``None`` for one that does not
    parse (the submit then falls back as for a token from another epoch)."""
    if not token:
        return None
    epoch, _, encoded = token.partition(".")
    if not epoch.isdigit() or not encoded:
        return None
    try:
        vv = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    return (int(epoch), vv) if vv else None


@dataclass(frozen=True, slots=True)
class PeerText:
    """A document's content, the token of the state it is, and for a submit
    the token of the state whose content is exactly the submitted text."""

    token: str
    text: str
    at_token: str | None = None
    #: The content of the state ``at_token`` names, when it is not the
    #: submitted text (a merge that kept everything holds more than was sent).
    at_text: str | None = None
    #: A retry of a submit already applied: ``at_token`` is not known.
    repeat: bool = False
    #: Whether the document's write back is landing: ``False`` while it is
    #: parked (saving paused), so the drive does not hold this text.
    saved: bool = True


@dataclass(frozen=True, slots=True)
class _Searched:
    """The closest state found for a text naming none, and when."""

    epoch: int
    base: bytes
    #: The states handed or made remembered at the search (encoded vectors).
    views: frozenset[str]


def _view_vvs(doc: CrdtDoc) -> list[str]:
    return [str(view["vv"]) for view in doc.text_peer_views or [] if "vv" in view]


Publish = Callable[[HubEvent], Awaitable[None]]

_V = TypeVar("_V")


def _remember(
    kept: OrderedDict[str, _V],
    key: str,
    value: _V,
    *,
    now: float,
    keep_for: float,
    at: Callable[[_V], float],
) -> None:
    """Remember ``value`` under ``key``, newest last, dropping what is older
    than ``keep_for`` (it would be read afresh anyway) and, past
    :data:`REMEMBERED_DOCS`, the oldest: a replica touches every document
    any of its sockets opened, and remembers only what it may still use."""
    kept[key] = value
    kept.move_to_end(key)
    while kept:
        oldest_key, oldest = next(iter(kept.items()))
        if len(kept) <= REMEMBERED_DOCS and now - at(oldest) < keep_for:
            break
        del kept[oldest_key]


@dataclass(frozen=True, slots=True)
class _SearchAgain:
    """The version a submit named is not one of the document's."""


_SEARCH_AGAIN: Final = _SearchAgain()

#: How many times a submit looks for its base with the row let go before it
#: gives up (the box then writes the file the ordinary way).
SEARCHES: Final = 3


@dataclass(frozen=True, slots=True)
class _Merged:
    """A merge a submit made and committed: what it is answered from."""

    applied: Applied
    epoch: int
    at_vv: bytes
    at_text: str | None
    keep: bool
    saved: bool


@dataclass
class TextPeers:
    """See the module docstring."""

    docs: CrdtDocs
    notify_delay: float = NOTIFY_DELAY_SECONDS
    publish: Publish | None = None
    """How a notice leaves this process; the ephemeral lane by default."""
    clock: Callable[[], float] = time.monotonic
    #: Submit answers by document and submit id: an id is the holder's to
    #: choose, so it is never a key on its own.
    _answers: OrderedDict[tuple[str, str], PeerText] = field(
        default_factory=OrderedDict, init=False
    )
    _notifying: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _again: set[str] = field(default_factory=set, init=False)
    _holders: OrderedDict[str, tuple[float, SourceHolder | None]] = field(
        default_factory=OrderedDict, init=False
    )
    #: When each document was last announced as opened, and the documents
    #: whose next notice goes to a holder that has not joined.
    _opened: OrderedDict[str, float] = field(default_factory=OrderedDict, init=False)
    _forced: set[str] = field(default_factory=set, init=False)
    #: The peer each document's last merge on this replica wrote as, by
    #: document and epoch (a peer never writes in two epochs).
    _merge_peers: OrderedDict[str, int] = field(default_factory=OrderedDict, init=False)

    # -- read and submit -------------------------------------------------------

    async def read(self, ref: DocRef) -> PeerText | None:
        """The document's content now and its token; ``None`` when no session
        is live on it (nothing opened it, or it was closed)."""
        if self.docs.type_of(ref).source is None:
            return None
        async with self.docs.session_factory() as db:
            doc = await self.docs.row(db, ref)
            if doc is None or doc.source_etag is None:
                return None
            async with self.docs.admitted():
                content, vv = await self.docs.latest(db, ref, doc)
            found = PeerText(token=encode_token(doc.epoch, vv), text=content)
            await self._handed(db, ref, doc, vv)
            await db.commit()
            return found

    async def submit(
        self,
        ref: DocRef,
        *,
        text: str,
        submit_id: str,
        agent_id: str | None,
        base_token: str | None = None,
        base_etag: int | None = None,
    ) -> PeerText | None:
        """Merge ``text`` into the document as an edit made on the state
        ``base_token`` names (see the module docstring). ``None`` when no
        session is live, or the text cannot be the document's (too large):
        the caller then writes the file the ordinary way."""
        remembered = self._answers.get((ref.key, submit_id))
        if remembered is not None:
            return remembered
        doc_type = self.docs.type_of(ref)
        if doc_type.source is None:
            return None
        update_id = f"{UPDATE_ID_PREFIX}{submit_id}"
        async with self.docs.session_factory() as db:
            # A base nobody named is searched for with the row unlocked (it
            # reads the document at many versions); only the merge holds it.
            named: tuple[str | None, int | None] = (base_token, base_etag)
            merged: PeerText | _Merged | _SearchAgain | None = _SEARCH_AGAIN
            for _ in range(SEARCHES):
                searched = await self._searched(db, ref, *named, text)
                merged = await self._submit_locked(
                    db,
                    ref,
                    text=text,
                    submit_id=submit_id,
                    update_id=update_id,
                    agent_id=agent_id,
                    named=named,
                    searched=searched,
                )
                if not isinstance(merged, _SearchAgain):
                    break
                # The version named is not one of the document's, or the
                # document moved to another epoch since the search: searched
                # for again as a base nobody named, with the row let go.
                named = (None, None)
        if merged is None or isinstance(merged, PeerText):
            return merged
        if isinstance(merged, _SearchAgain):
            log.info("crdt.text_peer.base_not_found", doc=ref.key)
            return None
        applied, epoch, at_vv, at_text = merged.applied, merged.epoch, merged.at_vv, merged.at_text
        keep, saved = merged.keep, merged.saved
        await self.docs.broadcast(ref, applied)
        await self.docs.after_commit(ref, applied)
        content = await self._content_at(ref, applied)
        log.info("crdt.text_peer.merged", doc=ref.key, keep=keep, agent_id=agent_id)
        if content is None:
            # The merged content could not be read back: the answer is the
            # state holding exactly what was sent, which the box then stands
            # on (never the merged token with the sent text, which would read
            # everything people typed since as removed by the next save).
            sent_at = encode_token(epoch, at_vv)
            held = text if at_text is None else at_text
            return PeerText(
                token=sent_at, text=held, at_token=sent_at, at_text=at_text, saved=saved
            )
        return self._keep(
            ref,
            submit_id,
            PeerText(
                token=encode_token(epoch, applied.vv),
                text=content,
                at_token=encode_token(epoch, at_vv),
                at_text=at_text,
                saved=saved,
            ),
        )

    async def _submit_locked(
        self,
        db: AsyncSession,
        ref: DocRef,
        *,
        text: str,
        submit_id: str,
        update_id: str,
        agent_id: str | None,
        named: tuple[str | None, int | None],
        searched: _Searched | None,
    ) -> PeerText | _Merged | _SearchAgain | None:
        """The part of a submit that holds the row: a repeat answered, the
        merge made and committed, or :data:`_SEARCH_AGAIN` when the version
        the holder named is not one of the document's (the row let go, so
        the search for a closer one never holds up anybody typing)."""
        doc_type = self.docs.type_of(ref)
        async with self.docs.admitted():
            doc = await self.docs.locked(db, ref)
            if doc is None or doc.source_etag is None:
                await db.rollback()
                return None
            sent_at = self._sent_at(doc, submit_id)
            if sent_at is not None or await self._logged(db, ref, doc, update_id):
                current = await self.docs.content(db, ref, doc, at=bytes(doc.vv))
                at_text = None
                if sent_at is not None:
                    at_text = await self._text_at(db, ref, doc, sent_at, text)
                saved = doc.save_paused_reason is None
                await db.commit()
                return PeerText(
                    token=encode_token(doc.epoch, bytes(doc.vv)),
                    text=current,
                    at_token=sent_at,
                    at_text=at_text,
                    repeat=True,
                    saved=saved,
                )
            found = await self._base(db, ref, doc, *named, text, searched)
            if found is None:
                await db.rollback()
                return _SEARCH_AGAIN
            base, keep = found
            doc.text_peer_until = _now() + timedelta(seconds=TEXT_PEER_SECONDS)
            try:
                reply, peer = await self._merge(db, ref, doc, base, text, keep=keep)
            except SandboxRefusedError as exc:
                if exc.code != "bad_base":
                    raise
                await db.rollback()
                return _SEARCH_AGAIN
            outcome = reply.header.get("outcome")
            delta, vv, projection, at_vv, sent_into = reply.blobs
            epoch = doc.epoch
            if outcome == "dup":
                # The document already holds this text at the base.
                current = await self.docs.content(db, ref, doc, at=bytes(doc.vv))
                saved = doc.save_paused_reason is None
                await db.commit()
                return self._keep(
                    ref,
                    submit_id,
                    PeerText(
                        token=encode_token(epoch, bytes(doc.vv)),
                        text=current,
                        at_token=encode_token(epoch, base),
                        saved=saved,
                    ),
                )
            if outcome != "ok":
                await db.rollback()
                log.info("crdt.text_peer.refused", doc=ref.key, reason=reply.header.get("reason"))
                return None
            # A merge that kept everything holds more than was sent when the
            # base had text the box's file did not: the box is told what the
            # state holds, or it would stand on that state with its own text
            # and its next save would read the difference as removed.
            held = sent_into.decode("utf-8")
            at_text = None if held == text else held
            # The box may hold either the merged text or, saving again
            # meanwhile, the state its text was merged at: both are
            # states it has.
            doc.text_peer_views = [
                self._view(doc, vv),
                PeerView(**self._view(doc, at_vv), submit=submit_id),
                *(doc.text_peer_views or []),
            ][:VIEWS_KEPT]
            applied = await self.docs.append(
                db,
                ref,
                doc,
                delta=delta,
                vv=vv,
                projection=decode_projection(projection),
                peer=peer,
                update_id=update_id,
                author=None,
                agent_id=agent_id,
                socket_peer_id=SERVER_PEER_ID,
                access=Access(
                    can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)
                ),
            )
            # Parked now, the write back that would take this text to
            # the drive is not running: the holder is told so, and
            # uploads its file the ordinary way.
            saved = doc.save_paused_reason is None
            await db.commit()
            self._merge_peers[self._peer_slot(ref, doc)] = peer
            self._merge_peers.move_to_end(self._peer_slot(ref, doc))
            while len(self._merge_peers) > MERGE_PEERS_KEPT:
                self._merge_peers.popitem(last=False)
            return _Merged(
                applied=applied, epoch=epoch, at_vv=at_vv, at_text=at_text, keep=keep, saved=saved
            )

    @staticmethod
    def _peer_slot(ref: DocRef, doc: CrdtDoc) -> str:
        return f"{ref.key}#{doc.epoch}"

    async def _merge(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, base: bytes, text: str, *, keep: bool
    ) -> tuple[Frame, int]:
        """The merge of ``text`` made on ``base``, and the peer it was written
        as: the peer this replica's last merge into this epoch wrote as, while
        the base holds everything it wrote (every peer a document has makes
        each later import into it dearer, and a peer per merge made a typed
        keystroke cost tens of milliseconds within minutes), else a new one."""
        reused = self._merge_peers.get(self._peer_slot(ref, doc))
        if reused is not None:
            try:
                return await self._merge_as(db, ref, doc, reused, base, text, keep=keep), reused
            except SandboxRefusedError as exc:
                if exc.code != "stale_peer":
                    raise
        peer = await self.docs.seed_peer(db)
        return await self._merge_as(db, ref, doc, peer, base, text, keep=keep), peer

    async def _merge_as(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        peer: int,
        base: bytes,
        text: str,
        *,
        keep: bool,
    ) -> Frame:
        return await self.docs.at_position(
            db,
            ref,
            doc,
            {
                "op": "merge",
                "key": self.docs.cache_key(ref, doc),
                "epoch": doc.epoch,
                "log_seq": doc.log_seq,
                "rules": self.docs.type_of(ref).rules,
                "peer": peer,
                "keep": keep,
            },
            [base, text.encode("utf-8")],
            maintenance=True,
        )

    @staticmethod
    def _named(doc: CrdtDoc, base_token: str | None, base_etag: int | None) -> bytes | None:
        """The version the writer named (its token, else the drive etag it
        agreed), when it is one of this epoch the session knows."""
        named = decode_token(base_token)
        if named is not None and named[0] == doc.epoch:
            return named[1]
        return None if base_etag is None else named_version(doc, base_etag)

    async def _searched(
        self,
        db: AsyncSession,
        ref: DocRef,
        base_token: str | None,
        base_etag: int | None,
        text: str,
    ) -> _Searched | None:
        """For a text whose writer named no version the session knows: the
        closest one, found without the row lock, with the epoch it is of and
        the states the holder had been handed by then."""
        async with self.docs.admitted():
            doc = await self.docs.row(db, ref)
            found = None
            if doc is not None and doc.source_etag is not None:
                if self._named(doc, base_token, base_etag) is None:
                    found = _Searched(
                        epoch=doc.epoch,
                        base=await self.docs.sessions.closest_base(db, ref, doc, text),
                        views=frozenset(_view_vvs(doc)),
                    )
            await db.commit()
        return found

    async def _nearer(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, searched: _Searched, text: str
    ) -> bytes:
        """The searched base, or a state a submit made after the search when
        the text is nearer it. Two sends of one disk naming no state (a box
        restarted while its first was still being served) both search before
        either lands; the second must merge from the state the first made, or
        what the first added is added again."""
        since = [
            decode_b64(view["vv"])
            for view in doc.text_peer_views or []
            if view.get("submit")
            and view.get("epoch") == doc.epoch
            and view.get("vv") not in searched.views
        ]
        if not since:
            return searched.base
        best, best_changed = searched.base, -1
        for candidate in [searched.base, *since]:
            try:
                there = await self.docs.content(db, ref, doc, at=candidate, maintenance=True)
            except SandboxRefusedError:
                continue
            changed = changed_between(there, text)
            if best_changed < 0 or changed < best_changed:
                best, best_changed = candidate, changed
        return best

    async def _base(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        base_token: str | None,
        base_etag: int | None,
        text: str,
        searched: _Searched | None,
    ) -> tuple[bytes, bool] | None:
        """The version the submitted text was made on, and whether it is
        merged keeping everything (a base nobody could name); ``None`` when
        it has to be searched for first (the search made before the row was
        taken is of another epoch, or there was none)."""
        named = self._named(doc, base_token, base_etag)
        if named is not None:
            return named, False
        if searched is not None and searched.epoch == doc.epoch:
            return await self._nearer(db, ref, doc, searched, text), True
        return None

    async def _content_at(self, ref: DocRef, applied: Applied) -> str | None:
        """The content at the version a submit produced, read once it is
        committed; ``None`` when the document moved to another epoch since."""
        try:
            async with self.docs.session_factory() as db:
                doc = await self.docs.row(db, ref)
                if doc is None or doc.epoch != applied.epoch:
                    return None
                async with self.docs.admitted():
                    return await self.docs.content(db, ref, doc, at=applied.vv)
        except Exception as exc:  # the answer falls back to the submitted text
            log.warning("crdt.text_peer.read_failed", doc=ref.key, error=safe_error(exc))
            return None

    async def _text_at(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, token: str, text: str
    ) -> str | None:
        """The content at ``token`` when it is not ``text``; ``None`` when it
        is, or cannot be read."""
        named = decode_token(token)
        if named is None or named[0] != doc.epoch:
            return None
        try:
            held = await self.docs.content(db, ref, doc, at=named[1], maintenance=True)
        except SandboxRefusedError:
            return None
        return None if held == text else held

    @staticmethod
    def _sent_at(doc: CrdtDoc, submit_id: str) -> str | None:
        """The token of the state a write already merged made (the one holding
        exactly what it sent), when this document remembers that write."""
        for view in doc.text_peer_views or []:
            if view.get("submit") == submit_id and view.get("epoch") == doc.epoch:
                try:
                    return encode_token(doc.epoch, decode_b64(view["vv"]))
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _view(doc: CrdtDoc, vv: bytes) -> PeerView:
        return PeerView(
            epoch=doc.epoch, vv=encode_b64(vv), etag=int(doc.source_etag or 0), at=time.time()
        )

    async def _handed(self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, vv: bytes) -> None:
        """Remember that the holder was handed the document at version
        ``vv``, and count it as the document's text peer a while longer. One
        row write, skipped when that version is remembered already and the
        mark is fresh (a box reading an unchanged document writes nothing)."""
        now = _now()
        view = self._view(doc, vv)
        fresh = doc.text_peer_until is not None and doc.text_peer_until > now + timedelta(
            seconds=TEXT_PEER_SECONDS - TEXT_PEER_REMARK_SECONDS
        )
        if fresh and any(seen.get("vv") == view["vv"] for seen in doc.text_peer_views or []):
            return
        await db.execute(
            update(CrdtDoc)
            .where(
                CrdtDoc.org_id == ref.org_id,
                CrdtDoc.doc_type == ref.stored_type,
                CrdtDoc.doc_id == ref.doc_id,
                CrdtDoc.epoch == doc.epoch,
            )
            .values(
                text_peer_until=now + timedelta(seconds=TEXT_PEER_SECONDS),
                text_peer_views=func.jsonb_path_query_array(
                    func.jsonb_build_array(literal(dict(view), JSONB)).op("||")(
                        CrdtDoc.text_peer_views
                    ),
                    cast(literal(f"$[0 to {VIEWS_KEPT - 1}]"), JSONPATH),
                ),
                # Not an edit: the session's age is left as it was.
                updated_at=CrdtDoc.updated_at,
            )
        )

    @staticmethod
    async def _logged(db: AsyncSession, ref: DocRef, doc: CrdtDoc, update_id: str) -> bool:
        found = await db.scalar(
            select(CrdtUpdate.log_seq)
            .where(
                CrdtUpdate.org_id == ref.org_id,
                CrdtUpdate.doc_type == ref.stored_type,
                CrdtUpdate.doc_id == ref.doc_id,
                CrdtUpdate.epoch == doc.epoch,
                CrdtUpdate.update_id == update_id,
            )
            .limit(1)
        )
        return found is not None

    def _keep(self, ref: DocRef, submit_id: str, answer: PeerText) -> PeerText:
        key = (ref.key, submit_id)
        self._answers[key] = answer
        self._answers.move_to_end(key)
        held = sum(len(kept.text) for kept in self._answers.values())
        while self._answers and (len(self._answers) > ANSWERS_KEPT or held > ANSWER_BYTES_KEPT):
            _, dropped = self._answers.popitem(last=False)
            held -= len(dropped.text)
        return answer

    # -- telling the holder ------------------------------------------------------

    def opened(self, ref: DocRef) -> None:
        """Somebody opened ``ref``: tell its holder even if it has not joined
        (it joins now, rather than at the first write back), at most once per
        :data:`OPEN_NOTICE_SECONDS` for one document."""
        if not self.docs.run_sessions or self.docs.type_of(ref).source is None:
            return
        now = self.clock()
        if now - self._opened.get(ref.key, -OPEN_NOTICE_SECONDS) < OPEN_NOTICE_SECONDS:
            return
        _remember(self._opened, ref.key, now, now=now, keep_for=OPEN_NOTICE_SECONDS, at=lambda t: t)
        self._forced.add(ref.key)
        self.changed(ref)

    def holder_wrote(self, ref: DocRef) -> None:
        """The holder of ``ref``'s folder wrote to the document itself (a box
        applying its agent's notebook operations): tell it once the document
        is still, joined or not. Only the holder writes the file while it
        holds the folder, and a box that edits through operations never read
        the file as text, so without this its own edit would reach the
        document and never the file. A holder that sends operations knows
        the notice, so it is told on every batch, not once a minute."""
        if not self.docs.run_sessions or self.docs.type_of(ref).source is None:
            return
        self._forced.add(ref.key)
        self.changed(ref)

    def changed(self, ref: DocRef) -> None:
        """A committed update to ``ref``: tell its holder once it is still."""
        if not self.docs.run_sessions or self.docs.type_of(ref).source is None:
            return
        running = self._notifying.get(ref.key)
        if running is not None and not running.done():
            self._again.add(ref.key)
            return
        task = asyncio.create_task(self._notify_later(ref))
        self._notifying[ref.key] = task

        def finished(done: asyncio.Task[None]) -> None:
            if self._notifying.get(ref.key) is done:
                del self._notifying[ref.key]
            if ref.key in self._again:
                self._again.discard(ref.key)
                self.changed(ref)

        task.add_done_callback(finished)

    async def _notify_later(self, ref: DocRef) -> None:
        await self.docs.sleep(self.notify_delay)
        forced = ref.key in self._forced
        self._forced.discard(ref.key)
        try:
            await self.notify(ref, joined_or_not=forced)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a lost notice: the box's write back path still runs
            log.warning("crdt.text_peer.notify_failed", doc=ref.key, error=safe_error(exc))

    async def notify(self, ref: DocRef, *, joined_or_not: bool = False) -> bool:
        """Tell the machine holding ``ref``'s folder that the document is at
        its current token. ``False`` when there is nobody to tell: no live
        session, no holder taking the file's writes, a holder that is not a
        registered machine (no channel), or one that has not joined lately
        (unless ``joined_or_not``: the document was just opened)."""
        source = self.docs.type_of(ref).source
        if source is None:
            return False
        async with self.docs.session_factory() as db:
            cached = self._holders.get(ref.key)
            if cached is not None and self.clock() - cached[0] < HOLDER_CACHE_SECONDS:
                holder = cached[1]
            else:
                holder = await source.holder(db, ref=ref)
                now = self.clock()
                _remember(
                    self._holders,
                    ref.key,
                    (now, holder),
                    now=now,
                    keep_for=HOLDER_CACHE_SECONDS,
                    at=lambda entry: entry[0],
                )
            doc = await self.docs.row(db, ref)
            await db.commit()
        if holder is None or doc is None or doc.source_etag is None:
            return False
        if not joined_or_not and (doc.text_peer_until is None or doc.text_peer_until <= _now()):
            # Nobody joined lately: the holder learns of the document from its
            # write backs, as a box from before text peers does.
            return False
        try:
            uuid.UUID(holder.machine_id)
        except ValueError:
            return False
        frame = LiveTextRequest(
            request_id=uuid.uuid4(),
            lease_node_id=holder.lease_node_id,
            epoch=holder.epoch,
            node_id=uuid.UUID(ref.doc_id),
            token=encode_token(doc.epoch, bytes(doc.vv)),
        )
        event = machine_event(ref.org_id, holder.machine_id, frame)
        await (self.publish or self._publish)(event)
        return True

    async def _publish(self, event: HubEvent) -> None:
        async with self.docs.session_factory() as db:
            await db.execute(text("SET LOCAL synchronous_commit = off"))
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": EPHEMERAL_CHANNEL, "payload": ephemeral_payload(event)},
            )
            await db.commit()

    async def aclose(self) -> None:
        tasks = [task for task in self._notifying.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._notifying.clear()


__all__ = [
    "NOTIFY_DELAY_SECONDS",
    "PeerText",
    "TextPeers",
    "decode_token",
    "encode_token",
]
