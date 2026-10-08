"""The document types the Loro CRDT lane serves, and what each one decides.

A CRDT document type is registered here, not hard-wired into the lane: the
gateway, the persistence and the sandbox ask the type, by name, everything
that differs between a chat's shared workspace and a file being co-edited —

* ``rules``: the rule set the sandbox judges an update against (which
  containers, which operations, which caps);
* ``limits``: how large an update and a document may grow, and when the log is
  folded into a snapshot or the history restarted;
* ``authorize``: who may read and who may write, decided afresh at every
  subscribe and every write;
* ``seed``: what a new document starts from (the draft a chat held before the
  lane; for a file, a version of the file on the drive);
* ``session_policy``: whether the CRDT document is the truth for as long as it
  lives (``persistent``) or only while it is being co-edited, flushed back to a
  source of truth at rest (``ephemeral_session``).

``chat_draft`` is the first type: one Loro document per chat, holding the
shared composer draft as a ``LoroText`` under ``draft`` (later containers join
it as the workspace grows), served to every org. ``file`` is a text file
co-edited live (:mod:`backend.services.crdt.sessions`), and ``notebook`` a
``.alknb.py`` notebook (:mod:`backend.services.crdt.notebook_type`), whose
document is a tree of cells rather than one text.

A type may also define ``on_write(db, *, ref, author, agent_id, notes)``: it
is awaited in the writer's transaction after a person's update is appended,
with what the sandbox said the update touched (the strategy's ``describe``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from alkera_core.auth.tenancy import member_stands
from alkera_core.authz import ActingContext, CredentialKind
from alkera_core.events import EventType
from alkera_core.models import RealtimeDoc, User
from alkera_core.models.crdt_doc import stored_doc_type
from alkera_core.schemas.realtime import decode_b64, encode_b64
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import files
from backend.services.crdt.errors import (
    CrdtError,
    SourceGoneError,
    SourceLandingError,
    SourceRefusedError,
    SourceStaleError,
)
from backend.services.crdt.file_kinds import file_name, live_type_of
from backend.services.realtime import channels as channel_rules
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.sharing import SharedRungCache

#: How long a peer waits before asking again for a file whose bytes are on
#: their way to the drive (a holder's upload lands within a second).
CONTENT_LANDING_RETRY_MS: Final = 250

SessionPolicy = Literal["persistent", "ephemeral_session"]


@dataclass(frozen=True, slots=True)
class DocRef:
    """One CRDT document: the org it belongs to, its type and its id."""

    org_id: UUID
    doc_type: str
    doc_id: str

    @property
    def key(self) -> str:
        """The sandbox's name for it (also what picks its worker)."""
        return f"{self.org_id}:{self.doc_type}:{self.doc_id}"

    @property
    def channel(self) -> str:
        return f"doc:{self.doc_type}:{self.doc_id}"

    @property
    def stored_type(self) -> str:
        """The value of the ``doc_type`` column this document's rows carry."""
        return stored_doc_type(self.doc_type)


@dataclass(frozen=True, slots=True)
class CrdtLimits:
    #: The largest raw update one write may carry.
    max_update_bytes: int
    #: Snapshot plus log; a write that would pass it is refused as ``doc_full``
    #: and the history is restarted from the current content.
    max_doc_bytes: int
    #: The soft cap a client holds typing to (0: none). The sandbox's hard cap
    #: sits above it so an honest client never meets the refusal.
    max_text_bytes: int
    #: Fold the log into a snapshot past either of these.
    compact_log_rows: int
    compact_log_bytes: int
    #: Restart the history (a new epoch) once the snapshot alone passes this.
    rotate_snapshot_bytes: int


@dataclass(frozen=True, slots=True)
class Access:
    """What one caller may do with one document right now. ``team_id`` and
    ``visibility`` address its fan-out exactly as the parent's grant does."""

    can_read: bool
    can_write: bool
    team_id: UUID | None = None
    owner_user_id: UUID | None = None
    visibility: str = "org"


DENIED = Access(can_read=False, can_write=False)


@dataclass(frozen=True, slots=True)
class DocPosition:
    """A point in a document's history: an epoch and a version vector in it."""

    epoch: int
    vv: bytes


@dataclass(frozen=True, slots=True)
class SourceStamp:
    """The version of a document's source at rest (a file on the drive) that
    the document's content matched: what a write back names as its
    precondition, and what an outside change is detected against."""

    #: The drive's etag for the file at that version.
    etag: int
    #: The version's id; ``None`` for a file that has no version yet.
    version_id: UUID | None
    #: SHA-256 (hex) of the content's UTF-8, compared with the document's.
    sha256: str
    #: The source version the writer of this one said it was made on (a
    #: drive version's if-match); ``None`` when it did not say.
    based_on: int | None = None
    #: Where in the document a write back took this version from, when a
    #: write back made it: what lets a pass recognise its own write.
    origin: DocPosition | None = None
    #: The machine whose agent wrote this version, when the folder's lease
    #: holder sent it: what a merge of it is attributed to.
    machine_id: str | None = None
    #: Its writer could not say which version it was made on (a box from
    #: before boxes did): merged from the closest of all the versions, and
    #: without removing anything.
    base_unknown: bool = False
    #: A restore of an older version, by this user: applied from exactly
    #: ``based_on`` (the closest text would be the restored version itself,
    #: from which the restore changes nothing), and written as them.
    restored_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class Seed:
    """What a new document's first epoch holds, and where it came from.
    ``stamp`` is set for a type whose truth at rest is a source the session
    writes back to (``ephemeral_session``). ``blank`` is an empty document
    that reads no text at all (a closed session's epoch): a type's reader
    may refuse an empty text, which a file holding nothing is, and must
    never be asked to read one for a document nobody's text started."""

    text: str
    source: str
    stamp: SourceStamp | None = None
    blank: bool = False


@dataclass(frozen=True, slots=True)
class SourceText:
    """A source's content now, and the version it is at."""

    text: str
    stamp: SourceStamp


@dataclass(frozen=True, slots=True)
class SourceHolder:
    """The machine holding a source's folder (and taking its writes): a text
    peer of the document is told on its channel when the document moves."""

    lease_node_id: UUID
    epoch: int
    machine_id: str


class DocSource(Protocol):
    """Where an ``ephemeral_session`` document's truth rests while nobody is
    editing it, and how the session reads and writes it. A type with a source
    registers one; the session engine (:mod:`backend.services.crdt.sessions`)
    writes the document back to it and merges changes made to it outside the
    session. A file's source is the drive; a notebook's would be its own."""

    async def read(self, db: AsyncSession, *, ref: DocRef) -> SourceText:
        """The content now. Raises :class:`SourceGoneError` when there is no
        longer anything a session can edit there."""
        ...

    async def etag(self, db: AsyncSession, *, ref: DocRef) -> int | None:
        """The source's version counter now (cheap: no content); ``None``
        when it is gone."""
        ...

    async def write(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        if_match: int,
        author: User,
        at: DocPosition | None = None,
    ) -> SourceStamp:
        """Write ``text`` as the source's next version, attributed to
        ``author``, provided it is still at ``if_match``. ``at`` is where in
        the document the text was taken from, recorded with the version (and
        read back as :attr:`SourceStamp.origin`). Raises
        :class:`SourceStaleError` when it moved, :class:`SourceRefusedError`
        when it will not take the write."""
        ...

    async def write_as_holder(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        if_match: int,
        machine_id: str | None,
        at: DocPosition | None = None,
    ) -> SourceStamp:
        """Write ``text`` as the source's next version as the machine holding
        its folder (the one whose agent made the unsaved edit, ``machine_id``,
        or whichever holds it when that is no longer known), under that
        holder's fence: what is written when the document's unsaved content
        is that machine's own and no person can be named for it. Raises as
        :meth:`write`; :class:`SourceRefusedError` (``leased``) when no such
        machine holds the folder."""
        ...

    async def keep_aside(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        author: User | None,
        machine_id: str | None = None,
    ) -> str:
        """Keep ``text`` beside the source as a copy of its own (a conflicted
        copy of the file): a session's content the source can no longer hold,
        kept before the session ends. Written as ``author``, or as the machine
        holding the source's folder when ``author`` is ``None`` (``machine_id``
        when it must be that one). The copy's id; raises
        :class:`SourceRefusedError` when it cannot be written."""
        ...

    def changed(self, event_type: str, payload: Mapping[str, Any]) -> str | None:
        """The document an announced event says this source changed for (a
        file's node on ``file_node.changed``), or ``None``. A session holding
        that document then looks for an outside change."""
        ...

    async def written(self, db: AsyncSession, *, ref: DocRef) -> list[tuple[int, DocPosition]]:
        """The latest versions a session wrote to the source, as ``(etag,
        position)``: what the source itself remembers about where in the
        document each was taken from, for a write back that landed and was
        cut off before the session recorded it. Newest first; empty for a
        source that keeps no such record."""
        ...

    async def holder(self, db: AsyncSession, *, ref: DocRef) -> SourceHolder | None:
        """The machine holding the source's folder and taking its writes, the
        one a text peer of the document runs on; ``None`` when none does."""
        ...


class CrdtDocType(Protocol):
    name: str
    rules: str
    doc_schema: int
    session_policy: SessionPolicy
    limits: CrdtLimits
    #: Where the document is written back to; ``None`` for a ``persistent``
    #: type, whose document is the truth.
    source: DocSource | None

    async def lock(self, db: AsyncSession, *, ref: DocRef) -> None:
        """Take whatever the type serialises against before the document row,
        for the transaction."""
        ...

    async def authorize(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        rungs: SharedRungCache | None = None,
    ) -> Access: ...

    async def seed(self, db: AsyncSession, *, ref: DocRef) -> Seed: ...

    async def recover(
        self, db: AsyncSession, *, ref: DocRef, projection: Mapping[str, Any]
    ) -> Seed:
        """What a document whose stored history no longer rebuilds starts its
        new epoch from (a quarantine). ``projection`` is the row's last one."""
        ...

    async def team_of(self, db: AsyncSession, *, ref: DocRef) -> UUID | None:
        """The team the document's parent narrows it to, for addressing a
        frame the server originates (a reload): the same team its writes are
        addressed to."""
        ...


class ChatWorkspaceType:
    """A chat's shared workspace: the composer draft every reader sees.

    Read by whoever may read the chat — the chat channel's own grant, asked
    through the same function, so the two can never disagree. Written only by
    a PERSON with edit rights on the chat: its owner, or someone it is shared
    with at ``writer`` or above. An agent (a socket asserting one, verified or
    not) and a box on its own credential are refused: a composer is where people talk to
    the agent, and an agent typing into it would be speaking as them.
    """

    name = "chat_draft"
    rules = "chat_draft"
    doc_schema = 1
    session_policy: SessionPolicy = "persistent"
    source: DocSource | None = None
    limits = CrdtLimits(
        # Well over a draft's 16 KiB (a paste, a burst typed while offline),
        # well under the history's room: an update that types and cuts more
        # than this spends the room a restart frees, and every restart reloads
        # every tab.
        max_update_bytes=128 * 1024,
        max_doc_bytes=2 * 1024 * 1024,
        max_text_bytes=16 * 1024,
        compact_log_rows=200,
        compact_log_bytes=256 * 1024,
        rotate_snapshot_bytes=512 * 1024,
    )

    async def lock(self, db: AsyncSession, *, ref: DocRef) -> None:
        # Nothing else writes a chat's draft: the document row's lock is enough.
        return None

    async def authorize(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        rungs: SharedRungCache | None = None,
    ) -> Access:
        if ent.org_id is None or ent.org_id != ref.org_id:
            return DENIED
        try:
            grant = await channel_rules.authorize(
                db,
                user,
                channel_rules.Channel(doc_type="chat", doc_id=ref.doc_id),
                ent=ent,
                # The chat's rules compare THIS with the chat's binding: a
                # bound machine reads the chat, and only a verified one is it.
                agent_id=machine_id,
                rungs=rungs,
            )
        except channel_rules.ChannelError:
            return DENIED
        return Access(
            can_read=True,
            can_write=grant.can_write and agent_id is None,
            team_id=grant.team_id,
            owner_user_id=grant.owner_user_id,
            visibility=grant.visibility,
        )

    async def seed(self, db: AsyncSession, *, ref: DocRef) -> Seed:
        """The draft the chat held before the lane (``meta.draft`` on its
        op-log document), or nothing. Read once, when the chat's workspace is
        first opened; the op-log lane refuses every draft write, so the text
        can no longer move after it."""
        state = (
            await db.execute(
                select(RealtimeDoc.state).where(
                    RealtimeDoc.org_id == ref.org_id,
                    RealtimeDoc.doc_type == "chat",
                    RealtimeDoc.doc_id == ref.doc_id,
                )
            )
        ).scalar_one_or_none()
        meta = state.get("meta") if isinstance(state, Mapping) else None
        draft = meta.get("draft") if isinstance(meta, Mapping) else None
        text = draft.get("text") if isinstance(draft, Mapping) else None
        if isinstance(text, str) and text:
            return Seed(text=text, source="legacy_draft")
        return Seed(text="", source="empty")

    async def recover(
        self, db: AsyncSession, *, ref: DocRef, projection: Mapping[str, Any]
    ) -> Seed:
        """The draft's last text, which every commit keeps in the projection
        (it is capped at 32 KiB, so keeping it there is cheap)."""
        text = projection.get("text")
        return Seed(text=text if isinstance(text, str) else "", source="quarantine")

    async def team_of(self, db: AsyncSession, *, ref: DocRef) -> UUID | None:
        scope = await channel_rules.lookup_chat_doc(db, org_id=ref.org_id, chat_id=ref.doc_id)
        return scope.team_id if scope is not None else None


#: How many of a file's latest write backs a merge may look back through:
#: enough to cover the session's source history window at the fastest rate
#: a session writes back (one burst of typing every couple of seconds over
#: fifteen minutes), so a box naming a version its agent read that long ago
#: is merged from that version rather than from a newer one.
WRITTEN_POSITIONS = 512


def _position(origin: Mapping[str, Any] | None) -> DocPosition | None:
    """A version's recorded write-back origin, or ``None`` when it has none
    or it does not parse."""
    if origin is None:
        return None
    epoch, vv = origin.get("epoch"), origin.get("vv")
    if not isinstance(epoch, int) or not isinstance(vv, str):
        return None
    try:
        return DocPosition(epoch=epoch, vv=decode_b64(vv))
    except ValueError:
        return None


def _node_of(ref: DocRef) -> UUID | None:
    """The file node a ``file`` document names, or ``None`` for an id that
    is not one."""
    try:
        return UUID(ref.doc_id)
    except ValueError:
        return None


def _reader(ref: DocRef, node_id: UUID) -> ActingContext:
    """Who the lane reads a file's bytes as once a person was admitted to
    it: the lane itself, scoped to the file's org. It decides nothing (the
    person's access was decided before) and writes nothing."""
    return ActingContext.for_service(
        token_id=node_id, org_id=ref.org_id, label="crdt_file", credential=CredentialKind.CI_TOKEN
    )


def _person(user: User, ref: DocRef) -> ActingContext:
    """``user`` acting in the document's org, which the caller has already
    found them standing in."""
    return ActingContext.for_user(user_id=user.id, org_id=ref.org_id, email=user.email)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FileSource:
    """A file on the drive, as a co-edited document's source.

    Read at its head version, as the lane; written back as a new version
    attributed to the person who wrote last, through the Files policy and
    under an etag precondition. When a box holds the chat folder's lease, the
    version lands as an inbound write the box applies through its existing
    inbound path: one writer on the box's disk, and an old box serves it. An
    edit only the box's agent made is written as that box, under its fence,
    as its own upload of it would be."""

    def __init__(self, *, max_bytes: int) -> None:
        self.max_bytes = max_bytes

    async def read(self, db: AsyncSession, *, ref: DocRef) -> SourceText:
        node_id = _node_of(ref)
        if node_id is None:
            raise SourceGoneError("gone")
        try:
            head = await files.read_head(
                db, _reader(ref, node_id), node_id, max_bytes=self.max_bytes
            )
        except files.NotEditableError as exc:
            raise SourceGoneError(exc.reason) from exc
        except files.NotLandedError as exc:
            raise SourceLandingError() from exc
        return SourceText(
            text=head.text,
            stamp=SourceStamp(
                etag=head.etag,
                version_id=head.version_id,
                sha256=_sha256(head.text),
                based_on=head.based_on,
                origin=_position(head.origin),
                machine_id=head.machine_id,
                base_unknown=head.base_unknown,
                restored_by=head.restored_by,
            ),
        )

    async def etag(self, db: AsyncSession, *, ref: DocRef) -> int | None:
        node_id = _node_of(ref)
        if node_id is None:
            return None
        return await files.head_etag(db, _reader(ref, node_id), node_id)

    async def write(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        if_match: int,
        author: User,
        at: DocPosition | None = None,
    ) -> SourceStamp:
        node_id = _node_of(ref)
        if node_id is None or not await member_stands(db, user=author, org_team_id=ref.org_id):
            raise SourceRefusedError("refused")
        origin = None if at is None else {"epoch": at.epoch, "vv": encode_b64(at.vv)}
        try:
            written = await files.write_back(
                db, _person(author, ref), node_id, text, if_match=if_match, origin=origin
            )
        except files.WriteBackRefusedError as exc:
            if exc.reason == "stale":
                raise SourceStaleError(exc.code) from exc
            raise SourceRefusedError(exc.reason, exc.code) from exc
        return SourceStamp(
            etag=written.etag, version_id=written.version_id, sha256=_sha256(text), origin=at
        )

    async def write_as_holder(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        if_match: int,
        machine_id: str | None,
        at: DocPosition | None = None,
    ) -> SourceStamp:
        node_id = _node_of(ref)
        if node_id is None:
            raise SourceRefusedError("refused")
        origin = None if at is None else {"epoch": at.epoch, "vv": encode_b64(at.vv)}
        try:
            written = await files.write_back_as_holder(
                db,
                ref.org_id,
                node_id,
                text,
                if_match=if_match,
                machine_id=machine_id,
                origin=origin,
            )
        except files.WriteBackRefusedError as exc:
            if exc.reason == "stale":
                raise SourceStaleError(exc.code) from exc
            raise SourceRefusedError(exc.reason, exc.code) from exc
        return SourceStamp(
            etag=written.etag, version_id=written.version_id, sha256=_sha256(text), origin=at
        )

    async def keep_aside(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        text: str,
        author: User | None,
        machine_id: str | None = None,
    ) -> str:
        node_id = _node_of(ref)
        if node_id is None:
            raise SourceRefusedError("refused")
        person = None
        if author is not None:
            if not await member_stands(db, user=author, org_team_id=ref.org_id):
                raise SourceRefusedError("refused")
            person = _person(author, ref)
        try:
            copy = await files.keep_aside(
                db, ref.org_id, node_id, text, person=person, machine_id=machine_id
            )
        except files.WriteBackRefusedError as exc:
            raise SourceRefusedError(exc.reason, exc.code) from exc
        return str(copy)

    async def written(self, db: AsyncSession, *, ref: DocRef) -> list[tuple[int, DocPosition]]:
        node_id = _node_of(ref)
        if node_id is None:
            return []
        found = await files.written_positions(
            db, _reader(ref, node_id), node_id, limit=WRITTEN_POSITIONS
        )
        return [
            (etag, position)
            for etag, origin in found
            if (position := _position(origin)) is not None
        ]

    async def holder(self, db: AsyncSession, *, ref: DocRef) -> SourceHolder | None:
        node_id = _node_of(ref)
        if node_id is None:
            return None
        found = await files.folder_holder(db, _reader(ref, node_id), node_id)
        if found is None:
            return None
        return SourceHolder(
            lease_node_id=found.lease_node_id, epoch=found.epoch, machine_id=found.machine_id
        )

    def changed(self, event_type: str, payload: Mapping[str, Any]) -> str | None:
        if event_type != EventType.FILE_NODE_CHANGED.value:
            return None
        node = payload.get("node_id")
        return node if isinstance(node, str) else None


class FileDocType:
    """A text file co-edited live: ``doc:file:<node_id>``.

    The document is the truth while a session is live and the file on the
    drive is the truth at rest (``ephemeral_session``). A session is seeded
    from the file's head version (``seeded_from`` names the version and its
    content hash), written back to the drive by the session engine, and
    merges changes made to the file outside the session.

    Read by anyone the Files policy lets read the node. Written by a PERSON
    the policy lets write it, and only while a write that holds no fence can
    land there (no live lease covers it, or the box's lease takes inbound
    writes at that path). An agent is refused writing here for now: it reaches
    the file on the box's disk, and the session merges what it writes there.
    Keyed by the node alone, so it is the same document wherever the file
    lives (a chat's working folder today, a workspace's tomorrow).
    """

    name = "file"
    rules = "file"
    doc_schema = 1
    session_policy: SessionPolicy = "ephemeral_session"
    limits = CrdtLimits(
        max_update_bytes=1024 * 1024,
        max_doc_bytes=8 * 1024 * 1024,
        # The largest file a session opens, and the soft cap a client holds
        # typing to; the sandbox's hard cap is twice it.
        max_text_bytes=1024 * 1024,
        compact_log_rows=500,
        compact_log_bytes=1024 * 1024,
        rotate_snapshot_bytes=4 * 1024 * 1024,
    )

    def __init__(self) -> None:
        self._files = FileSource(max_bytes=self.limits.max_text_bytes)
        self.source: DocSource | None = self._files

    async def lock(self, db: AsyncSession, *, ref: DocRef) -> None:
        return None

    async def authorize(
        self,
        db: AsyncSession,
        *,
        ref: DocRef,
        user: User,
        ent: EntitlementSnapshot,
        agent_id: str | None,
        machine_id: str | None = None,
        rungs: SharedRungCache | None = None,
    ) -> Access:
        node_id = _node_of(ref)
        # The connection's org (its credential's), never the person's home org.
        if node_id is None or ent.org_id is None or ent.org_id != ref.org_id:
            return DENIED
        access = await files.document_access(db, _person(user, ref), node_id)
        if not access.can_read:
            return DENIED
        return Access(can_read=True, can_write=access.can_write and agent_id is None)

    async def seed(self, db: AsyncSession, *, ref: DocRef) -> Seed:
        await self._refuse_other_kinds(db, ref)
        head = await self._read(db, ref)
        source = (
            f"file_version:{head.stamp.version_id}:{head.stamp.sha256}"
            if head.stamp.version_id is not None
            else "file_empty"
        )
        return Seed(text=head.text, source=source, stamp=head.stamp)

    async def recover(
        self, db: AsyncSession, *, ref: DocRef, projection: Mapping[str, Any]
    ) -> Seed:
        """The file as the drive holds it: the edits since the last write
        back cannot be trusted to rebuild, and the drive is the truth. What
        the broken history still holds was kept beside the file first (the
        session engine's rescue, which the store runs for every type with a
        source before it asks this)."""
        head = await self._read(db, ref)
        return Seed(text=head.text, source="quarantine", stamp=head.stamp)

    async def _refuse_other_kinds(self, db: AsyncSession, ref: DocRef) -> None:
        """A notebook is co-edited as a notebook document, never as plain text
        beside it (two documents would each write the one file back)."""
        name = await file_name(db, ref.org_id, ref.doc_id)
        if name is not None and live_type_of(name) != self.name:
            raise CrdtError(
                "not_editable", "this file is co-edited as another kind", reason=live_type_of(name)
            )

    async def team_of(self, db: AsyncSession, *, ref: DocRef) -> UUID | None:
        # Only sockets that hold the channel hear its frames, and each was
        # admitted by the Files policy: no team narrows it further.
        return None

    async def _read(self, db: AsyncSession, ref: DocRef) -> SourceText:
        try:
            return await self._files.read(db, ref=ref)
        except SourceGoneError as exc:
            raise CrdtError(
                "not_editable", "this file cannot be edited live", reason=exc.reason
            ) from exc
        except SourceLandingError as exc:
            raise CrdtError(
                "content_landing",
                "the file's content has not reached the drive yet",
                retry_after_ms=CONTENT_LANDING_RETRY_MS,
            ) from exc


#: How each type a default registry serves is made, in registration order.
#: The lane's own types are here; a type module adds its own at import
#: (:func:`register_type`), and the lane's store imports every such module.
_TYPE_FACTORIES: list[Callable[[], CrdtDocType]] = [ChatWorkspaceType, FileDocType]


def register_type(factory: Callable[[], CrdtDocType]) -> None:
    """Serve the type ``factory`` makes in every default registry."""
    if factory not in _TYPE_FACTORIES:
        _TYPE_FACTORIES.append(factory)


class CrdtRegistry:
    """The registered types, by name."""

    def __init__(self, types: Mapping[str, CrdtDocType] | None = None) -> None:
        if types is None:
            types = {made.name: made for made in (factory() for factory in _TYPE_FACTORIES)}
        self._types: dict[str, CrdtDocType] = dict(types)

    def get(self, name: str) -> CrdtDocType | None:
        return self._types.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._types)


__all__ = [
    "DENIED",
    "Access",
    "ChatWorkspaceType",
    "CrdtDocType",
    "CrdtLimits",
    "CrdtRegistry",
    "DocPosition",
    "DocRef",
    "DocSource",
    "EntitlementSnapshot",
    "FileDocType",
    "FileSource",
    "Seed",
    "SessionPolicy",
    "SourceHolder",
    "SourceStamp",
    "SourceText",
    "register_type",
]
