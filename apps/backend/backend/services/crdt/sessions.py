"""Sessions of documents whose truth at rest is a source (``ephemeral_session``).

A ``file`` document is the truth only while people edit it; at rest the file
on the drive is. This module keeps the two in step, for any type that
registers a :class:`~backend.services.crdt.registry.DocSource`:

* **Write back.** The document's content is written to the source as a new
  version, under a precondition on the version the document last matched
  (``crdt_docs.source_etag``), attributed to the person who wrote last (or,
  when no person can be named and the unsaved edit is the agent's, merged
  through the text peer, written as the machine holding the folder). It
  runs debounced after edits, when the last editor leaves, and before an idle
  session is compacted. It holds the document's row lock throughout, so two
  replicas never write one document back at once, and an edit that commits
  meanwhile waits for it.
* **Outside changes.** When the source moved past the document's stamp (a
  box pushing the agent's edit, a REST upload, a restore), the change is read
  and merged into the document as one edit by a server peer, made on the
  version whose content the source held before the change: the people's live
  edits stay and the outside change lands around them. A write back that
  meets a stale precondition merges first and writes the merged content.
* **Closing.** A source with nothing editable left (the file was deleted,
  grew past the cap or became binary) ends the session: the document is
  restarted empty and forgets its source, and opening it again reads the
  source afresh. Never over content the source does not hold: a session
  whose file was trashed is parked until a restore lets it write back (a
  purge deletes it), and one whose file stopped being text first keeps its
  content beside the file as a conflicted copy.
* **Saving state.** Whether a session's edits are reaching its source lives
  on its row (``crdt_docs.save_paused_reason``): set when a write back is
  refused, cleared by the write back that lands. Every replica announces
  from the row, so a pause one replica announced is lifted by whichever
  replica's write back lands.

Who writes the file on a box's disk: when a box holds the chat folder's lease
(and its lease takes inbound writes there), the write back is an inbound
write the box applies under its own fence, through the path a file dropped in
the browser takes. Nothing here talks to the box.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from alkera_core.auth.tenancy import member_stands
from alkera_core.doc_type_names import STORED_DOC_TYPES
from alkera_core.logging import get_logger
from alkera_core.models import CrdtDoc, CrdtPeer, CrdtUpdate, User
from alkera_core.models.crdt_doc import SourceRecord
from alkera_core.schemas.realtime import SERVER_PEER_ID, decode_b64, encode_b64
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt import sweeper
from backend.services.crdt.errors import (
    CrdtError,
    SourceGoneError,
    SourceLandingError,
    SourceRefusedError,
    SourceStaleError,
    safe_error,
)
from backend.services.crdt.registry import (
    Access,
    DocPosition,
    DocRef,
    DocSource,
    Seed,
    SourceStamp,
    SourceText,
)
from backend.services.crdt.sandbox.pool import SandboxError, SandboxRefusedError
from backend.services.crdt.sandbox.protocol import decode_projection
from backend.services.infra import now as _now

if TYPE_CHECKING:
    from backend.services.crdt.docs import Applied, CrdtDocs

log = get_logger(__name__)

#: How long after an edit the document is written back. A burst of typing is
#: written back once, this long after it began.
WRITE_BACK_DELAY_SECONDS: Final = 2.0
#: How long a session nobody writes to waits before it is written back and
#: compacted to its content alone.
IDLE_SECONDS: Final = 60.0
#: What a closed session's epoch is seeded as: nothing, and no source.
CLOSED: Final = "closed"
#: How long a session remembers an earlier source version, to merge an outside
#: change made on it: a window of time, not a count, so a burst of write backs
#: never pushes out the version a slow writer (an agent mid-turn, an editor
#: left open) read.
SOURCE_HISTORY_SECONDS: Final = 15 * 60.0
#: The most earlier versions a session keeps however fast it writes back: the
#: bound on the row, the oldest going first.
SOURCE_HISTORY_MAX: Final = 512
#: How many people a write back is offered to before saving is paused.
AUTHOR_CANDIDATES: Final = 16
#: How many remembered versions an outside change is compared with, nearest
#: (to the version its writer named) first.
BASE_CANDIDATES: Final = 24
#: The saving-paused reason a quarantine that lost edits records and announces.
QUARANTINED_LOST: Final = "quarantined_lost"
#: The update id a text peer's submit is logged under (``peer-<submit id>``).
TEXT_PEER_UPDATE_PREFIX: Final = "peer-"
#: The update id a batch of document operations is logged under (a notebook
#: peer's: an agent, or the machine holding the folder).
OPS_UPDATE_PREFIX: Final = "nbops-"

Outcome = Literal["none", "unchanged", "written", "merged", "stale", "refused", "closed", "busy"]


@dataclass(frozen=True, slots=True)
class Settled:
    """What one pass did. ``applied`` is an outside change merged into the
    document, which the caller announces once its transaction commits."""

    outcome: Outcome
    applied: Applied | None = None


def _wire_type(stored: str) -> str:
    """The document type a stored ``doc_type`` names (a renamed type keeps
    its legacy spelling at rest)."""
    return {legacy: current for current, legacy in STORED_DOC_TYPES.items()}.get(stored, stored)


def _settle(doc: CrdtDoc, read: _ToWrite) -> bool:
    """Put the hash of ``read``'s content in ``doc``'s projection when the
    row is still at the position it was read at; whether it changed it."""
    if doc.epoch != read.epoch or doc.log_seq != read.log_seq:
        return False
    projection = dict(doc.projection) if isinstance(doc.projection, dict) else {}
    digest = sha256_of(read.text)
    if projection.get("sha256") == digest:
        return False
    projection["sha256"] = digest
    doc.projection = projection
    return True


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(doc: CrdtDoc, stamp: SourceStamp, *, vv: bytes, now: float) -> bool:
    """The document's content at ``vv`` (this epoch) is the source at ``stamp``;
    the version it matched until now joins the history, which keeps what was
    the latest within :data:`SOURCE_HISTORY_SECONDS` of ``now``. Answers
    whether this lifted a pause on saving (the row said saving was paused)."""
    history = list(doc.source_history or [])
    if doc.source_etag is not None and doc.source_vv is not None and doc.source_epoch == doc.epoch:
        previous = SourceRecord(
            etag=int(doc.source_etag),
            version_id=None if doc.source_version_id is None else str(doc.source_version_id),
            vv=encode_b64(bytes(doc.source_vv)),
            at=now,
        )
        if previous["etag"] != stamp.etag:
            history = [previous, *history]
    else:
        history = []
    # A record from before ``at`` was kept ages out with the next one that has it.
    horizon = now - SOURCE_HISTORY_SECONDS
    history = [r for r in history if float(r.get("at", now)) >= horizon]
    doc.source_etag = stamp.etag
    doc.source_version_id = stamp.version_id
    doc.source_sha256 = stamp.sha256
    doc.source_vv = vv
    doc.source_epoch = doc.epoch
    doc.source_history = history[:SOURCE_HISTORY_MAX]
    # The source took (or already held) this content: whatever parked the
    # session is over.
    return _unpark(doc)


def _unpark(doc: CrdtDoc) -> bool:
    """Clear the row's parking; whether it was parked (saving was paused)."""
    paused = doc.save_paused_reason is not None
    doc.save_paused_reason = None
    doc.save_failures = 0
    doc.save_retry_at = None
    return paused


def content_sha(doc: CrdtDoc) -> str | None:
    """The hash of the session's content now, as its projection records it."""
    projection = doc.projection if isinstance(doc.projection, dict) else {}
    sha = projection.get("sha256")
    return sha if isinstance(sha, str) else None


def holds_unsaved(doc: CrdtDoc) -> bool:
    """Whether the session holds content its source does not: its content
    hash is not the one it last matched (the sweep's own predicate)."""
    sha = content_sha(doc)
    return sha is not None and sha != doc.source_sha256


async def _snapshot(db: AsyncSession) -> None:
    """Begin a read that sees one committed state throughout: the row and
    the update log it names agree even when a compaction or another writer
    commits while the read runs, and no lock is taken."""
    await db.commit()
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


def named_version(doc: CrdtDoc, etag: int) -> bytes | None:
    """The document's version at source version ``etag`` in its current
    epoch, when the session remembers it (its latest, or one in its history)."""
    if doc.source_vv is None or doc.source_epoch != doc.epoch:
        return None
    return _named(doc, bytes(doc.source_vv), etag)


def _named(doc: CrdtDoc, current: bytes, etag: int) -> bytes | None:
    """The document's version at source version ``etag``, when the session
    remembers it (its latest, or one in its history)."""
    if etag == int(doc.source_etag or 0):
        return current
    for record in doc.source_history or []:
        if record.get("etag") == etag:
            try:
                return decode_b64(record["vv"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _views(doc: CrdtDoc, taken: set[bytes]) -> list[tuple[int, bytes]]:
    """The states of this epoch the document's text peer was handed lately
    (what a machine's file may hold rather than a write back), as base
    candidates ``(etag current then, version)``, leaving out ``taken``."""
    found: list[tuple[int, bytes]] = []
    for view in doc.text_peer_views or []:
        try:
            if int(view["epoch"]) != doc.epoch:
                continue
            vv = decode_b64(view["vv"])
            etag = int(view.get("etag", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if vv not in taken:
            taken.add(vv)
            found.append((etag, vv))
    return found


def _spread(items: list[tuple[int, bytes]], budget: int) -> list[tuple[int, bytes]]:
    """At most ``budget`` of ``items``, evenly spread from first to last (both
    kept), in their order."""
    if len(items) <= budget:
        return items
    step = (len(items) - 1) / (budget - 1)
    return [items[round(n * step)] for n in range(budget)]


def changed_between(before: str, after: str) -> int:
    """How much of two texts differs: their lengths past a shared start and
    end. Small when one is the other plus an edit."""
    limit = min(len(before), len(after))
    start = 0
    while start < limit and before[start] == after[start]:
        start += 1
    end = 0
    while end < limit - start and before[-1 - end] == after[-1 - end]:
        end += 1
    return (len(before) - start - end) + (len(after) - start - end)


@dataclass(slots=True)
class Flushed:
    """What a flush before a trash did: the files it wrote back (etag before
    and after), and those whose edits it could not."""

    moved: dict[str, tuple[int, int]] = field(default_factory=dict)
    unsaved: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MachineEdit:
    """This epoch holds an edit the machine holding the source's folder
    merged through the text peer: ``machine_id`` is the machine, ``None``
    when its update rows were folded away and only the submit is on record."""

    machine_id: str | None


@dataclass(frozen=True, slots=True)
class _ToWrite:
    """What one write back writes, as read: the content at ``vv`` of epoch
    ``epoch``, made on the source version ``source_etag``."""

    epoch: int
    source_etag: int
    vv: bytes
    text: str
    unchanged: bool
    authors: list[User]
    #: The row's position when it was read.
    log_seq: int = 0
    #: Whether the row's projection already carried the content's hash.
    settled: bool = True
    #: The holder's own agent edited this epoch: what is written as that
    #: machine when no person in ``authors`` may write it.
    machine: MachineEdit | None = None
    #: The row said saving was paused when this was read.
    paused: bool = False


#: The refusals of a write back as one person that the next person in the
#: session may not meet: the policy refusing them (they lost the right to
#: write the file, or never stood in the org), and their own storage limit.
#: A drive at its ceiling, or the org's quota, refuses everyone alike.
_PERSONAL_REFUSALS: Final = frozenset(
    {"", "files.not_writable", "files.forbidden", "files.user_quota_bytes"}
)


@dataclass
class SessionSync:
    """Write back, merge and close; see the module docstring. A pass takes
    the caller's session and commits it between its phases, so no lock is
    held across the sandbox or the source; it returns what the caller
    announces after its own final commit."""

    docs: CrdtDocs
    clock: Callable[[], float] = time.time
    """The wall clock the source history's window is measured on."""

    def _source(self, ref: DocRef) -> DocSource | None:
        return self.docs.type_of(ref).source

    async def write_back(self, db: AsyncSession, ref: DocRef) -> Settled:
        """Write the document back to its source when its content moved
        since the source last matched it.

        Attributed to the person who wrote last, or, when the drive will not
        take it as them (they lost the right to write the file), to the next
        person in the session who still may: everyone holding a writing peer
        on it, then everyone who wrote in it. When nobody may, or the source
        refuses the write for another reason (a machine holds the folder and
        takes no outside write), every subscriber is told saving is paused
        and why, and told again once a later pass lands.

        No lock is held across the slow parts. The content is read from the
        sandbox and the write made to the source (its own transaction, which
        commits the drive's version and releases its locks at once) with the
        document row unlocked, so people typing into it, and every request
        that touches the drive, never queue behind a write back. Only the
        record of what the source now holds takes the row, briefly, and only
        when nothing moved the source meanwhile."""
        source = self._source(ref)
        if source is None:
            return Settled("none")
        merged: Applied | None = None
        for attempt in range(2):
            read = await self._read(db, ref)
            if read is None:
                return Settled("none", merged)
            if read.unchanged:
                if not read.settled:
                    await self._settle_projection(db, ref, read)
                if read.paused:
                    await self._unchanged_resumes(db, ref, read)
                return Settled("unchanged", merged)
            written = await self._write(ref, source, read)
            if written == "stale":
                if attempt:
                    return Settled("stale", merged)
                settled = await self.merge_outside(db, ref)
                if settled.outcome in ("closed", "refused"):
                    return settled
                merged = settled.applied or merged
                continue
            if isinstance(written, str):
                await self._paused(ref, read.epoch, written)
                return Settled("refused", merged)
            doc = await self.docs.locked(db, ref)
            resumed = False
            if (
                doc is not None
                and doc.epoch == read.epoch
                and doc.source_etag is not None
                and int(doc.source_etag) == read.source_etag
            ):
                resumed = _record(doc, written, vv=read.vv, now=self.clock())
                _settle(doc, read)
                await db.flush()
            log.info("crdt.session.written_back", doc=ref.key, etag=written.etag)
            if resumed:
                await self._resumed(ref, read.epoch)
            return Settled("written", merged)
        return Settled("stale", merged)  # pragma: no cover - the loop returns

    async def _settle_projection(self, db: AsyncSession, ref: DocRef, read: _ToWrite) -> None:
        """The content read is what the source holds: record its hash in the
        row's projection when the row is still where it was read (a type
        whose per-update projection carries no rendered hash, a notebook,
        then reads as saved)."""
        doc = await self.docs.locked(db, ref)
        if doc is not None and _settle(doc, read):
            await db.flush()

    async def _read(self, db: AsyncSession, ref: DocRef) -> _ToWrite | None:
        """What a write back writes, read without a lock, and who it may be
        written as; the read's transaction ends here, so nothing it saw is
        held while the source is written."""
        async with self.docs.admitted():
            await _snapshot(db)
            doc = await self.docs.row(db, ref)
            if doc is None or doc.source_etag is None:
                await db.commit()
                return None
            text, vv = await self.docs.latest(db, ref, doc, maintenance=True)
            unchanged = sha256_of(text) == doc.source_sha256
            read = _ToWrite(
                epoch=doc.epoch,
                source_etag=int(doc.source_etag),
                vv=vv,
                text=text,
                unchanged=unchanged,
                authors=[] if unchanged else await self._authors(db, ref, doc),
                log_seq=doc.log_seq,
                settled=(
                    isinstance(doc.projection, dict)
                    and doc.projection.get("sha256") == sha256_of(text)
                ),
                machine=None if unchanged else await _machine_edit(db, ref, doc),
                paused=doc.save_paused_reason is not None,
            )
            await db.commit()
            return read

    async def _write(
        self, ref: DocRef, source: DocSource, read: _ToWrite
    ) -> SourceStamp | Literal["stale"] | str:
        """Write ``read`` to the source as the first author it takes it as,
        each try in its own committed transaction, then, when no person
        could and the machine holding the folder edited this epoch, as that
        machine. The stamp written, or ``stale``, or why nobody could
        (``no_writer``, ``leased``, a code)."""
        async with self.docs.session_factory() as wdb:
            gone = await source.etag(wdb, ref=ref) is None
            await wdb.commit()
        if gone:
            # Trashed (a purge deletes the session with the file): parked
            # until a restore lets it write back.
            return "gone"
        written = await self._write_as_people(ref, source, read)
        if read.machine is None or written not in ("no_writer", "files.user_quota_bytes"):
            return written
        refusal = written
        async with self.docs.session_factory() as wdb:
            try:
                stamp = await source.write_as_holder(
                    wdb,
                    ref=ref,
                    text=read.text,
                    if_match=read.source_etag,
                    machine_id=read.machine.machine_id,
                    at=DocPosition(epoch=read.epoch, vv=read.vv),
                )
            except SourceStaleError:
                return "stale"
            except SourceRefusedError as exc:
                log.info(
                    "crdt.session.holder_write_back_refused",
                    doc=ref.key,
                    reason=exc.reason,
                    code=exc.code,
                )
                return refusal
            await wdb.commit()
        log.info("crdt.session.written_back_as_holder", doc=ref.key, etag=stamp.etag)
        return stamp

    async def _write_as_people(
        self, ref: DocRef, source: DocSource, read: _ToWrite
    ) -> SourceStamp | Literal["stale"] | str:
        """:meth:`_write` as the people in ``read.authors`` alone."""
        refusal = "no_writer"
        for author in read.authors:
            async with self.docs.session_factory() as wdb:
                try:
                    stamp = await source.write(
                        wdb,
                        ref=ref,
                        text=read.text,
                        if_match=read.source_etag,
                        author=author,
                        at=DocPosition(epoch=read.epoch, vv=read.vv),
                    )
                except SourceStaleError:
                    return "stale"
                except SourceRefusedError as exc:
                    log.info(
                        "crdt.session.write_back_refused",
                        doc=ref.key,
                        reason=exc.reason,
                        code=exc.code,
                    )
                    if exc.reason == "refused" and exc.code in _PERSONAL_REFUSALS:
                        # This person may not write the file now: the next may.
                        if exc.code == "files.user_quota_bytes":
                            refusal = exc.code
                        continue
                    # Anything else refuses whoever writes (the folder's
                    # holder takes no outside write, the drive is at its
                    # ceiling): the next person would only be refused again.
                    return "leased" if exc.reason == "leased" else (exc.code or exc.reason)
                await wdb.commit()
                return stamp
        return refusal

    async def _paused(self, ref: DocRef, epoch: int, reason: str) -> None:
        """A write back was refused: park the session for the sweep, and tell
        its readers, once per reason rather than once per try."""
        fresh = await sweeper.park(self.docs.session_factory, ref, reason=reason, now=_now())
        if not fresh:
            return
        log.warning("crdt.session.saving_paused", doc=ref.key, reason=reason)
        await self.docs.announce_saving(ref, epoch=epoch, paused=True, reason=reason)

    async def _resumed(self, ref: DocRef, epoch: int) -> None:
        """A pass lifted a pause the row recorded (whichever replica parked
        it): tell the readers saving landed again."""
        log.info("crdt.session.saving_resumed", doc=ref.key)
        await self.docs.announce_saving(ref, epoch=epoch, paused=False)

    async def _unchanged_resumes(self, db: AsyncSession, ref: DocRef, read: _ToWrite) -> None:
        """A parked session whose content is the source's again (its edits
        undone, or the source took them another way): nothing is left to
        save, so the pause is lifted."""
        doc = await self.docs.locked(db, ref)
        if doc is None or doc.epoch != read.epoch or holds_unsaved(doc):
            return
        if _unpark(doc):
            await db.flush()
            await self._resumed(ref, read.epoch)

    async def flush_under(self, org_id: UUID, node_id: UUID) -> dict[str, tuple[int, int]]:
        """Write back now every file session at or under ``node_id`` holding
        edits not yet on the drive (before a trash takes the file away from
        its session). Answers, per file that moved, its etag before and after.
        A write back that is refused or fails leaves that file as it was:
        see :meth:`flush_report` for which."""
        return (await self.flush_report(org_id, node_id)).moved

    async def flush_report(self, org_id: UUID, node_id: UUID) -> Flushed:
        """:meth:`flush_under`, and the files whose edits it could not write
        back. Those sessions are not lost with the trash: a session whose
        file is trashed is parked (never closed) until a restore lets it
        write back."""
        async with self.docs.session_factory() as db:
            found = await sweeper.unsaved_under(
                db,
                org_id=org_id,
                node_id=node_id,
                doc_types=sweeper.sourced_types(self.docs.registry),
            )
        flushed = Flushed()
        for doc_type, doc_id, before in found:
            ref = DocRef(org_id, _wire_type(doc_type), doc_id)
            try:
                async with self.docs.session_factory() as db:
                    settled = await self.write_back(db, ref)
                    await db.commit()
                    row = await self.docs.row(db, ref)
            except Exception as exc:  # the trash goes ahead regardless
                log.warning("crdt.sessions.flush_failed", doc=ref.key, error=safe_error(exc))
                flushed.unsaved.append(doc_id)
                continue
            if settled.outcome == "written" and row is not None and row.source_etag is not None:
                flushed.moved[doc_id] = (before, int(row.source_etag))
            elif settled.outcome != "unchanged":
                flushed.unsaved.append(doc_id)
        if flushed.unsaved:
            log.warning("crdt.sessions.flush_left_unsaved", docs=flushed.unsaved)
        return flushed

    async def merge_outside(self, db: AsyncSession, ref: DocRef) -> Settled:
        """Merge a change made to the source outside the session, if there is
        one (a cheap etag read when there is not).

        The source is read, and the version the change was made on found,
        with the document row unlocked; the row is taken only to merge and
        record, and only when the session still knows the source as it did
        when the pass began (another pass that settled it meanwhile wins)."""
        source = self._source(ref)
        if source is None:
            return Settled("none")
        await _snapshot(db)
        doc = await self.docs.row(db, ref)
        if doc is None or doc.source_etag is None:
            return Settled("none")
        etag = await source.etag(db, ref=ref)
        if etag is not None and etag == doc.source_etag:
            return Settled("unchanged")
        try:
            if etag is None:
                raise SourceGoneError("gone")
            head = await source.read(db, ref=ref)
        except SourceGoneError as exc:
            return await self._end(db, ref, reason=exc.reason)
        except SourceLandingError:
            # The source's next bytes are still on their way: settled once
            # they land, never from the empty text they are not.
            return Settled("unchanged")
        if head.stamp.origin is not None and head.stamp.origin.epoch == doc.epoch:
            # A write back of this very document (this replica's or another's)
            # that was cut off before it recorded itself: what the source holds
            # is the document at that point, so it is recorded, never merged
            # in again (merging it would repeat every edit since the last
            # record).
            return await self._recognise(
                db, ref, doc, head.stamp, vv=head.stamp.origin.vv, event="own_write"
            )
        if (
            doc.source_epoch == doc.epoch
            and doc.source_vv is not None
            and head.stamp.sha256 == doc.source_sha256
        ):
            # Only the etag moved: the source holds the very content the
            # session last matched (a rename, a box handing back the bytes it
            # was just sent). It is recorded at the position that content is,
            # never merged: merged again from the version its writer named, a
            # change already in the document would be applied a second time.
            return await self._recognise(
                db, ref, doc, head.stamp, vv=bytes(doc.source_vv), event="same_content"
            )
        base = bytes(doc.source_vv) if doc.source_vv is not None else b""
        if doc.source_epoch != doc.epoch or not base:
            # Every new epoch records where its source stands, so this is a
            # bug, not a state: the outside change is then merged as an edit
            # of the current content, and the live edits since the last write
            # back are what it may overwrite.
            log.error("crdt.session.base_lost", doc=ref.key, epoch=doc.epoch)
            base = b""
        else:
            async with self.docs.admitted():
                base = await self._base_for(db, ref, doc, base, head)
        epoch, known = doc.epoch, int(doc.source_etag)
        await db.commit()
        async with self.docs.admitted():
            doc = await self.docs.locked(db, ref)
            if (
                doc is None
                or doc.epoch != epoch
                or doc.source_etag is None
                or int(doc.source_etag) != known
            ):
                return Settled("unchanged")
            settled = await self._merge(db, ref, doc, head, base or bytes(doc.vv))
        if isinstance(settled, str):
            return await self._end(db, ref, reason=settled)
        return settled

    async def idle(self, db: AsyncSession, ref: DocRef) -> Settled:
        """A session nobody holds a writing peer on: write it back, and when
        that leaves the source matching it, restart it as its content alone
        (the history and the peers go; a tab still reading rebases)."""
        doc = await self.docs.row(db, ref)
        if doc is None or doc.source_etag is None or await self._held(db, ref):
            return Settled("none")
        settled = await self.write_back(db, ref)
        if settled.outcome not in ("unchanged", "written"):
            return settled
        doc = await self.docs.locked(db, ref)
        if doc is None or doc.log_seq == 0:
            # Nothing written since the epoch began: already its content alone.
            return settled
        async with self.docs.admitted():
            await self.docs.restart(db, ref, reason="idle", quarantine=False)
        return settled

    async def reopen(self, db: AsyncSession, ref: DocRef) -> bool:
        """A closed session asked for again: seed a new epoch from the source
        when it holds something editable now (raises the type's refusal when
        it does not). ``False`` when the document was not closed. Looked at
        without the row lock first: every hello on a file asks this."""
        seen = await self.docs.row(db, ref)
        if seen is None or seen.source_etag is not None or seen.seeded_from != CLOSED:
            return False
        doc = await self.docs.locked(db, ref)
        if doc is None or doc.source_etag is not None or doc.seeded_from != CLOSED:
            return False
        doc_type = self.docs.type_of(ref)
        seed = await doc_type.seed(db, ref=ref)
        await self.docs.restart(db, ref, reason="reopened", quarantine=False, seed=seed)
        return True

    async def _recognise(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        stamp: SourceStamp,
        *,
        vv: bytes,
        event: str,
    ) -> Settled:
        """Record the source at ``stamp``, whose content is the document's at
        ``vv`` (a version this document wrote, or the content it already
        matched), provided the session still knows the source as it did."""
        epoch, known = doc.epoch, doc.source_etag
        await db.commit()
        locked = await self.docs.locked(db, ref)
        if locked is not None and locked.epoch == epoch and locked.source_etag == known:
            if _record(locked, stamp, vv=vv, now=self.clock()):
                await self._resumed(ref, epoch)
            await db.flush()
            log.info(f"crdt.session.recognised_{event}", doc=ref.key, etag=stamp.etag)
        return Settled("unchanged")

    async def _merge(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, head: SourceText, base: bytes
    ) -> Settled | str:
        """Merge ``head`` (made on the version at ``base``) into the locked
        document ``doc`` and record the source at it; the reason when the
        sandbox refuses to (the session then ends, see :meth:`_end`)."""
        peer = await self.docs.seed_peer(db)
        reply = await self.docs.at_position(
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
                # Its writer could not name the version it was made on: what
                # it lacks may only be what it never saw, so nothing is removed.
                "keep": head.stamp.base_unknown,
            },
            [base, head.text.encode("utf-8")],
            maintenance=True,
        )
        outcome = reply.header.get("outcome")
        delta, vv, projection, base_vv, *_ = reply.blobs
        if outcome == "dup":
            if _record(doc, head.stamp, vv=base, now=self.clock()):
                await self._resumed(ref, doc.epoch)
            await db.flush()
            return Settled("unchanged")
        if outcome != "ok":
            return str(reply.header.get("reason") or "refused")
        doc_type = self.docs.type_of(ref)
        applied = await self.docs.append(
            db,
            ref,
            doc,
            delta=delta,
            vv=vv,
            projection=decode_projection(projection),
            peer=peer,
            update_id=f"source-{head.stamp.etag}",
            # A restore is the restorer's edit; anything else outside, nobody's.
            author=(
                None
                if head.stamp.restored_by is None
                else await db.get(User, head.stamp.restored_by)
            ),
            # The agent that wrote the change on its machine, when the lease
            # holder sent it: the editors draw it as that agent's edit.
            agent_id=head.stamp.machine_id,
            socket_peer_id=SERVER_PEER_ID,
            access=Access(
                can_read=True, can_write=False, team_id=await doc_type.team_of(db, ref=ref)
            ),
        )
        if _record(doc, head.stamp, vv=base_vv, now=self.clock()):
            await self._resumed(ref, doc.epoch)
        await db.flush()
        log.info("crdt.session.merged_outside_change", doc=ref.key, etag=head.stamp.etag)
        return Settled("merged", applied)

    async def _base_for(
        self,
        db: AsyncSession,
        ref: DocRef,
        doc: CrdtDoc,
        current: bytes,
        head: SourceText,
    ) -> bytes:
        """The version an outside change was made on.

        Of the source versions this session matched (the latest, then those
        of the last :data:`SOURCE_HISTORY_SECONDS`), the one the new text
        differs least from; on a tie, the one its writer named (``based_on``,
        a drive version's if-match), else the latest. The versions nearest the
        named one are the ones compared, when there are more than the budget.

        The named version is a hint, never a bound. A box names the etag it
        last agreed with the drive, which can be older than what its file
        holds (its own upload not yet agreed) or newer (an agent that read the
        file before the session's last write back reached the box's disk, and
        wrote it after, made its change on an older version). Merged from a
        version older than the text's, every line since reads as added again
        and the document is duplicated; merged from one newer, the person's
        later edits read as removed. The closest text is the version it was
        made on in both cases."""
        based_on = head.stamp.based_on
        if based_on is not None and (
            head.stamp.restored_by is not None
            or (head.stamp.machine_id is None and not head.stamp.base_unknown)
        ):
            # Written by a person or an app naming the version it read (a
            # save through the API, a restore), not by a box holding the
            # folder: what it left out it removed, so it is merged from exactly
            # that version, never from the closest text (an older version the
            # removed text is not in, or for a restore the restored version
            # itself). A box's named version is a hint (its file can be older
            # or newer than the version it last agreed); see below.
            bound = _named(doc, current, based_on)
            if bound is not None:
                return bound
            if head.stamp.restored_by is not None:
                return current
        candidates: list[tuple[int, bytes]] = [(int(doc.source_etag or 0), current)]
        for record in doc.source_history or []:
            try:
                candidates.append((int(record["etag"]), decode_b64(record["vv"])))
            except (KeyError, TypeError, ValueError):
                continue
        # And what the source remembers this document writing, in this epoch:
        # a write back that landed and was cut off before it was recorded is
        # one the session does not know it made, and an edit made on top of it
        # is merged from it, not from the version before.
        source = self._source(ref)
        if source is not None:
            known = {etag for etag, _ in candidates}
            for etag, position in await source.written(db, ref=ref):
                if position.epoch == doc.epoch and etag not in known:
                    candidates.append((etag, position.vv))
        if head.stamp.base_unknown:
            # Its writer could not say which version it was made on (and may
            # have named the head regardless): no version is nearer than
            # another, so the ones compared are spread over all of them.
            based_on = None
            candidates = _spread(sorted(candidates, key=lambda one: -one[0]), BASE_CANDIDATES)
        else:
            # Nearest the named version first (else the latest), and only so
            # many: each costs the sandbox a checkout.
            candidates = sorted(
                candidates,
                key=lambda one: (
                    (abs(one[0] - based_on), -one[0]) if based_on is not None else -one[0]
                ),
            )[:BASE_CANDIDATES]
        if head.stamp.machine_id is not None or head.stamp.base_unknown:
            candidates.extend(_views(doc, {vv for _, vv in candidates}))
        best, best_rank = current, (-1, False)
        best_changed = -1
        for etag, candidate in candidates:
            try:
                text_there = await self.docs.content(db, ref, doc, at=candidate, maintenance=True)
            except SandboxRefusedError:
                continue
            changed = changed_between(text_there, head.text)
            named = etag == based_on
            if (
                best_changed < 0
                or changed < best_changed
                or (changed == best_changed and named and not best_rank[1])
            ):
                best, best_changed, best_rank = candidate, changed, (etag, named)
        if best is not current:
            log.info("crdt.session.merged_from_older_version", doc=ref.key, etag=best_rank[0])
        return best

    async def closest_base(self, db: AsyncSession, ref: DocRef, doc: CrdtDoc, text: str) -> bytes:
        """The version a text whose writer could not name one was most likely
        made on (see :meth:`_base_for`): the closest of every version the
        session remembers, compared as for an outside change from a writer
        that named no base."""
        current = (
            bytes(doc.source_vv)
            if doc.source_vv is not None and doc.source_epoch == doc.epoch
            else bytes(doc.vv)
        )
        unknown = SourceStamp(
            etag=int(doc.source_etag or 0), version_id=None, sha256="", base_unknown=True
        )
        # Every drive version the session remembers is compared, however
        # long ago it stopped being the latest. Being handed a state is no
        # sign the box wrote it to its disk: it writes one only when its
        # disk has not moved since, and a box sending its own text read the
        # document and found its disk had moved. Leaving out the versions
        # older than the first state handed left out the one the disk held,
        # and its typed-into lines came back beside their typed copies. The
        # comparisons stay within the search's budget (BASE_CANDIDATES).
        return await self._base_for(db, ref, doc, current, SourceText(text=text, stamp=unknown))

    async def _end(self, db: AsyncSession, ref: DocRef, *, reason: str) -> Settled:
        """The source holds nothing the session can edit now (``reason``):
        end the session, but never over content the source does not hold.

        A trashed file (``gone``) parks the session: a restore lets the sweep
        write it back, and a purge deletes it with the file. A file that grew
        past the cap, became binary, or that the sandbox refused to merge
        first gets the session's content beside it as a conflicted copy, and
        the session ends only once that copy holds what it holds; when no
        copy could be written, it is parked with the reason instead."""
        await db.commit()
        doc = await self.docs.row(db, ref)
        await db.commit()
        if doc is None or doc.source_etag is None:
            return Settled("none")
        kept: str | None = None
        if holds_unsaved(doc):
            if reason == "gone":
                if doc.save_paused_reason != reason:
                    await self._paused(ref, doc.epoch, reason)
                return Settled("refused")
            kept = await self._keep_aside(db, ref, reason=reason)
            if kept is None:
                if doc.save_paused_reason != reason:
                    await self._paused(ref, doc.epoch, reason)
                return Settled("refused")
        async with self.docs.admitted():
            locked = await self.docs.locked(db, ref)
            if locked is None or locked.source_etag is None:
                return Settled("none")
            if holds_unsaved(locked) and content_sha(locked) != kept:
                # Somebody typed after the copy was taken: the next pass
                # keeps that too before the session may end.
                await db.rollback()
                return Settled("refused")
            return await self._close(db, ref, reason=reason)

    async def _keep_aside(self, db: AsyncSession, ref: DocRef, *, reason: str) -> str | None:
        """Write the session's content beside its source as a conflicted
        copy (:meth:`_write_aside`). The content hash kept, or ``None`` when
        nobody could."""
        async with self.docs.admitted():
            await _snapshot(db)
            doc = await self.docs.row(db, ref)
            if doc is None or doc.source_etag is None:
                await db.commit()
                return None
            text, _vv = await self.docs.latest(db, ref, doc, maintenance=True)
            writers = await self._aside_writers(db, ref, doc)
            await db.commit()
        if not await self._write_aside(ref, text, writers, reason=reason):
            return None
        return sha256_of(text)

    async def _aside_writers(
        self, db: AsyncSession, ref: DocRef, doc: CrdtDoc
    ) -> list[User | MachineEdit]:
        """Who a copy kept beside the source may be written as, in order: the
        people a write back may be attributed to, then the machine holding
        the folder when its agent edited this epoch."""
        authors = await self._authors(db, ref, doc)
        machine = await _machine_edit(db, ref, doc)
        return [*authors, *([machine] if machine else [])]

    async def _write_aside(
        self, ref: DocRef, text: str, writers: list[User | MachineEdit], *, reason: str
    ) -> bool:
        """Write ``text`` beside the source as a conflicted copy, as the first
        of ``writers`` who may, each try in its own transaction. Whether one
        could."""
        source = self._source(ref)
        if source is None:
            return False
        for writer in writers:
            async with self.docs.session_factory() as wdb:
                try:
                    copy = await source.keep_aside(
                        wdb,
                        ref=ref,
                        text=text,
                        author=writer if isinstance(writer, User) else None,
                        machine_id=writer.machine_id if isinstance(writer, MachineEdit) else None,
                    )
                except (SourceRefusedError, SourceGoneError, SourceStaleError) as exc:
                    log.info("crdt.session.keep_aside_refused", doc=ref.key, error=str(exc))
                    await wdb.rollback()
                    continue
                await wdb.commit()
            log.warning("crdt.session.kept_aside", doc=ref.key, reason=reason, copy=copy)
            return True
        return False

    async def rescue(self, db: AsyncSession, ref: DocRef, doc: CrdtDoc) -> bool:
        """Before a quarantine restarts ``doc`` (locked by the caller) from its
        source: keep what it held that the source does not.

        The history is rebuilt in the sandbox as far as it goes (the snapshot,
        then every logged update that still imports; nothing is cached and
        the hot path pays nothing for it). When the rebuilt text is not the
        source's, it is kept beside the source as a conflicted copy
        (``crdt.doc.quarantine_rescued``). When the rebuild fails, or no copy
        can be written, and the session held edits the source lacks, they
        are lost: logged as ``crdt.doc.quarantine_lost_edits`` and recorded
        on the row as saving paused (:data:`QUARANTINED_LOST`), which
        :meth:`announce_lost` tells the open tabs once the restart is in.
        Answers whether edits were lost. Never raises."""
        source = self._source(ref)
        if source is None:
            return False
        try:
            head = await source.read(db, ref=ref)
        except (SourceGoneError, SourceLandingError):
            # Nothing to compare with or keep beside: the quarantine's own
            # recovery refuses it the same way.
            return False
        except Exception as exc:
            log.warning("crdt.doc.quarantine_rescue_failed", doc=ref.key, error=safe_error(exc))
            return False
        rebuilt = await self._rebuilt(db, ref, doc)
        held = (doc.source_sha256, head.stamp.sha256)
        if rebuilt is not None and sha256_of(rebuilt) in held:
            # The source holds it already (as last written back, or now).
            return False
        if rebuilt is not None:
            try:
                writers = await self._aside_writers(db, ref, doc)
                kept = await self._write_aside(ref, rebuilt, writers, reason="quarantine")
            except Exception as exc:
                log.warning("crdt.doc.quarantine_rescue_failed", doc=ref.key, error=safe_error(exc))
                kept = False
            if kept:
                log.warning("crdt.doc.quarantine_rescued", doc=ref.key, epoch=doc.epoch)
                return False
        elif content_sha(doc) in (None, *held):
            # Its last recorded content is the source's: nothing was lost.
            return False
        log.error(
            "crdt.doc.quarantine_lost_edits",
            doc=ref.key,
            epoch=doc.epoch,
            rebuilt=rebuilt is not None,
        )
        doc.save_paused_reason = QUARANTINED_LOST
        doc.save_failures = 0
        doc.save_retry_at = None
        return True

    async def announce_lost(self, ref: DocRef, *, epoch: int) -> None:
        """Tell the open tabs a quarantine lost the edits since the last
        write back (:meth:`rescue`). The row says it too, until the next
        write back lands and announces saving resumed."""
        await self.docs.announce_saving(ref, epoch=epoch, paused=True, reason=QUARANTINED_LOST)

    async def _rebuilt(self, db: AsyncSession, ref: DocRef, doc: CrdtDoc) -> str | None:
        """The document's content rebuilt from its stored snapshot and every
        logged update that still applies; ``None`` when it cannot be."""
        rows = (
            await db.execute(
                select(CrdtUpdate.data)
                .where(
                    CrdtUpdate.org_id == ref.org_id,
                    CrdtUpdate.doc_type == ref.stored_type,
                    CrdtUpdate.doc_id == ref.doc_id,
                    CrdtUpdate.epoch == doc.epoch,
                    CrdtUpdate.log_seq > doc.snapshot_log_seq,
                )
                .order_by(CrdtUpdate.log_seq)
            )
        ).scalars()
        header = {
            "op": "salvage",
            "key": self.docs.cache_key(ref, doc),
            "epoch": doc.epoch,
            "rules": self.docs.type_of(ref).rules,
        }
        try:
            reply = await self.docs.sandbox_request(
                ref,
                header,
                [bytes(doc.snapshot or b""), *(bytes(row) for row in rows)],
                budget=self.docs.load_budget,
            )
            text = reply.blobs[0].decode("utf-8")
        except (CrdtError, SandboxError, IndexError, UnicodeDecodeError) as exc:
            log.warning("crdt.doc.quarantine_rebuild_failed", doc=ref.key, error=safe_error(exc))
            return None
        log.info(
            "crdt.doc.quarantine_rebuilt",
            doc=ref.key,
            applied=reply.header.get("applied"),
            skipped=reply.header.get("skipped"),
        )
        return text

    async def _close(self, db: AsyncSession, ref: DocRef, *, reason: str) -> Settled:
        """End a session whose source holds nothing editable any more. The
        caller holds an admission (the restart works in the sandbox)."""
        log.info("crdt.session.closed", doc=ref.key, reason=reason)
        await self.docs.restart(
            db,
            ref,
            reason=f"closed:{reason}",
            quarantine=False,
            seed=Seed(text="", source=CLOSED, blank=True),
        )
        return Settled("closed")

    async def _authors(self, db: AsyncSession, ref: DocRef, doc: CrdtDoc) -> list[User]:
        """Who a write back may be attributed to, in order: the person who
        wrote last, then whoever holds a writing peer on the document now
        (most recently seen first), then whoever wrote in this epoch (most
        recently first). Only active members of the document's org; the drive
        decides, per write, which of them may still write the file."""
        ordered: list[UUID] = []
        projection = doc.projection if isinstance(doc.projection, dict) else {}
        try:
            ordered.append(UUID(str(projection.get("by_user_id"))))
        except ValueError:
            pass
        holders = await db.scalars(
            select(CrdtPeer.user_id)
            .where(
                CrdtPeer.org_id == ref.org_id,
                CrdtPeer.doc_type == ref.doc_type,
                CrdtPeer.doc_id == ref.doc_id,
                CrdtPeer.held_by.is_not(None),
                CrdtPeer.held_until >= func.now(),
            )
            .order_by(CrdtPeer.last_seen_at.desc())
            .limit(AUTHOR_CANDIDATES)
        )
        ordered.extend(holders)
        writers = await db.execute(
            select(CrdtUpdate.author_user_id, func.max(CrdtUpdate.log_seq).label("last"))
            .where(
                CrdtUpdate.org_id == ref.org_id,
                CrdtUpdate.doc_type == ref.doc_type,
                CrdtUpdate.doc_id == ref.doc_id,
                CrdtUpdate.epoch == doc.epoch,
                CrdtUpdate.author_user_id.is_not(None),
            )
            .group_by(CrdtUpdate.author_user_id)
            .order_by(func.max(CrdtUpdate.log_seq).desc())
            .limit(AUTHOR_CANDIDATES)
        )
        ordered.extend(row.author_user_id for row in writers)
        authors: list[User] = []
        seen: set[UUID] = set()
        for user_id in ordered:
            if user_id in seen or len(authors) >= AUTHOR_CANDIDATES:
                continue
            seen.add(user_id)
            user = await db.get(User, user_id)
            if (
                user is not None
                and user.is_active
                and await member_stands(db, user=user, org_team_id=ref.org_id)
            ):
                authors.append(user)
        return authors

    @staticmethod
    async def _held(db: AsyncSession, ref: DocRef) -> bool:
        """Whether a socket holds a writing peer on the document now."""
        held = await db.scalar(
            select(func.count())
            .select_from(CrdtPeer)
            .where(
                CrdtPeer.org_id == ref.org_id,
                CrdtPeer.doc_type == ref.stored_type,
                CrdtPeer.doc_id == ref.doc_id,
                CrdtPeer.held_by.is_not(None),
                CrdtPeer.held_until >= func.now(),
            )
        )
        return bool(held)


async def _machine_edit(db: AsyncSession, ref: DocRef, doc: CrdtDoc) -> MachineEdit | None:
    """Whether the machine holding the folder merged an edit into this epoch
    through the text peer, and which machine: its latest submit still in the
    log, else a submit the document remembers handing back (the log folded
    since), else a batch of operations the holder applied as itself (a
    notebook's: logged for no person, under the machine of its lease; an
    agent's batch always names the person it acts for)."""
    agent = await db.scalar(
        select(CrdtUpdate.agent_id)
        .where(
            CrdtUpdate.org_id == ref.org_id,
            CrdtUpdate.doc_type == ref.stored_type,
            CrdtUpdate.doc_id == ref.doc_id,
            CrdtUpdate.epoch == doc.epoch,
            CrdtUpdate.update_id.startswith(TEXT_PEER_UPDATE_PREFIX, autoescape=True),
            CrdtUpdate.agent_id.is_not(None),
        )
        .order_by(CrdtUpdate.log_seq.desc())
        .limit(1)
    )
    if agent is not None:
        return MachineEdit(machine_id=str(agent))
    if any(
        view.get("submit") and view.get("epoch") == doc.epoch for view in doc.text_peer_views or []
    ):
        return MachineEdit(machine_id=None)
    holder = await db.scalar(
        select(CrdtUpdate.agent_id)
        .where(
            CrdtUpdate.org_id == ref.org_id,
            CrdtUpdate.doc_type == ref.stored_type,
            CrdtUpdate.doc_id == ref.doc_id,
            CrdtUpdate.epoch == doc.epoch,
            CrdtUpdate.update_id.startswith(OPS_UPDATE_PREFIX, autoescape=True),
            CrdtUpdate.author_user_id.is_(None),
            CrdtUpdate.agent_id.is_not(None),
        )
        .order_by(CrdtUpdate.log_seq.desc())
        .limit(1)
    )
    if holder is not None:
        return MachineEdit(machine_id=str(holder))
    return None


__all__ = [
    "AUTHOR_CANDIDATES",
    "CLOSED",
    "IDLE_SECONDS",
    "OPS_UPDATE_PREFIX",
    "QUARANTINED_LOST",
    "SOURCE_HISTORY_MAX",
    "SOURCE_HISTORY_SECONDS",
    "TEXT_PEER_UPDATE_PREFIX",
    "WRITE_BACK_DELAY_SECONDS",
    "Flushed",
    "MachineEdit",
    "Outcome",
    "SessionSync",
    "Settled",
    "changed_between",
    "content_sha",
    "holds_unsaved",
    "named_version",
    "sha256_of",
]
