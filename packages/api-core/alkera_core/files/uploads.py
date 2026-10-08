"""Upload sessions: open, parts, status, complete, abort.

The session row is written before the first byte reaches the store, so every
staged object has an owner: a client that dies mid-upload leaves rows the
sweeper can finish, never an object nobody can name. The three facts that make
a 10 GB upload resumable across a closed laptop lid live on that row: the
declared size, the bytes received so far and ``expires_at``, bumped on every
accepted part so an upload in progress never expires under its client.

Two properties the tests pin:

* **A re-sent part is decided by its checksum, never by arrival order.** The
  part rows are keyed ``(session_id, part_no)``, so a resend either matches the
  accepted part (a no-op that touches neither the store nor the byte counter)
  or disagrees with it and is refused. Two backends racing the same part number
  therefore leave exactly one row, whichever won the insert.
* **Every state change is one compare-and-swap.** ``open|uploading →
  uploading`` and ``* → aborted`` are single ``UPDATE … WHERE state = …``
  statements, so an abort racing a part is decided by Postgres rather than by
  whichever coroutine read the row first, and both can never win.

``complete`` agrees on the parts and queues the commit; the commit operation
turns them into a version.
"""

from __future__ import annotations

import contextlib
import json
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from sqlalchemy import CursorResult, Table, func, select, text, update

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files import names
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.conflicts import free_conflicted_copy_name
from alkera_core.files.drives import assert_traversal_rules
from alkera_core.files.errors import (
    Conflict,
    FilesError,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
    StoreUnavailable,
    TooLarge,
)
from alkera_core.files.history import subject_ref
from alkera_core.files.ids import DriveId, NodeId, OperationId, SessionId, VersionId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import (
    HolderIdentity,
    HolderKind,
    LeaseContext,
    admitted_inbound,
    covering_lease,
    fenced_write_for,
    is_final_push,
    is_hand_back,
)
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import Operations, OperationState, Progress
from alkera_core.files.quota import CeilingsResolver, QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store import keys
from alkera_core.files.store.errors import ChecksumMismatch, StoreError, Throttled, Unavailable
from alkera_core.files.store.errors import InvalidRequest as StoreInvalidRequest
from alkera_core.files.store.scoped import DomainStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import (
    UPLOAD_SESSION_LIVE_STATES,
    FileUploadPart,
    FileUploadSession,
)

if TYPE_CHECKING:  # pragma: no cover - the commit path imports this module back
    from alkera_core.files.content import VersionInfo

#: Re-run the caller's authorization facts before the bytes move. It is handed
#: the folder the version will land in and raises the refusal it decides on;
#: returning is the allow.
Reauthorize = Callable[[NodeId], Awaitable[None]]

#: The part size every proxied session is cut into: the deployment's own
#: setting rather than a second literal, so what ``limits`` publishes and what
#: a part PUT enforces cannot drift apart. It is one half of the real file
#: ceiling (the other is ``files_max_upload_parts``) and every hop between a
#: browser and the store carries one body this size, so it moves only together
#: with the edge — see ``files_part_max_bytes`` in :mod:`alkera_core.config`
#: for the full list of what must move with it.
PART_SIZE: Final = settings.files_part_max_bytes
#: How long a session lives without progress; bumped on every accepted part.
SESSION_TTL_INTERVAL: Final = "7 days"
#: The states a part may be accepted in.
PART_ACCEPTING_STATES: Final[tuple[str, ...]] = ("open", "uploading")

_NO_CHECKPOINTS: Final[Checkpoints] = NoopCheckpoints()
_SESSIONS: Final[Table] = cast(Table, FileUploadSession.__table__)

#: What a commit does when the folder already holds the session's name.
#: ``fail``/``rename`` are the namespace's own vocabulary — they decide how a
#: *new* node takes a name. ``replace`` is the content PUT's: it lands the
#: bytes as a new version on the live same-name file instead of a second node
#: beside it, which is the only way a file larger than the single-call PUT cap
#: can have its bytes replaced at all.
CommitBehavior = Literal["fail", "rename", "replace"]


def _deadline() -> Any:
    """The TTL as a Postgres expression: ``now()`` decides deadlines, not us."""
    return func.now() + text(f"interval '{SESSION_TTL_INTERVAL}'")


def part_key(session_id: SessionId, part_no: int, checksum: bytes) -> str:
    """Where one part's bytes are staged while the session is in flight.

    Addressed by the part's checksum as well as its number. A part streams with
    no lock held, so two requests may send the same part at once; with the
    checksum in the key, different bytes never land on the same object, and the
    part row — which records the checksum — names exactly the object it kept.
    """
    return keys.incoming_key(session_id, f"part-{part_no}-{checksum.hex()}")


def legacy_part_key(session_id: SessionId, part_no: int) -> str:
    """Where a session staged before checksum-addressed keys keeps a part.

    Sessions live for days, so one opened before checksum-addressed keys may
    still be completed or aborted; every reader of a staged part also looks
    here.
    """
    return keys.incoming_key(session_id, f"part-{part_no}")


def staged_part_keys(session_id: SessionId, part_no: int, checksum: bytes) -> tuple[str, str]:
    """Every key a recorded part may be staged under, the current one first."""
    return part_key(session_id, part_no, checksum), legacy_part_key(session_id, part_no)


def parts_total(declared_size: int, part_size: int = PART_SIZE) -> int:
    """How many parts a declared size is cut into.

    A zero-byte file is one part, not none: a session with no parts has nothing
    for ``complete`` to agree with the store about, so an empty file is sent as
    a single zero-byte part and lands as a single empty object. The store's
    single-object path writes it (an S3 endpoint gets one empty ``PutObject``,
    never a multipart upload with no parts, which S3 refuses).
    """
    return max(1, math.ceil(declared_size / part_size))


#: The unit ladder the ceiling is said in: decimal, because storage is stated in
#: the units it is sold in everywhere else in the product (the rule, and the
#: general formatter, live in ``alkera_core.units``; the ladder is restated here
#: rather than reached for because it stops at GB on purpose): the general
#: formatter would step 1000 GB to ``1 TB``, the same size under a name the
#: product does not use. The composer's ``describeUploadLimit``
#: is the same ladder in TypeScript, so one ceiling never reads as two figures.
_LIMIT_UNITS: Final[tuple[str, ...]] = ("B", "KB", "MB", "GB")


def max_upload_size_label(maximum: int) -> str:
    """The file ceiling as the sentence a reader is refused with says it.

    The default reads as ``1000 GB`` — the figure the Files page and the
    composer show — and a ceiling under a gigabyte falls down the same ladder
    (``100 B``, ``2.5 MB``).
    """
    value = float(maximum)
    index = 0
    while value >= 1000 and index < len(_LIMIT_UNITS) - 1:
        value /= 1000
        index += 1
    if index == 0:
        return f"{int(value)} {_LIMIT_UNITS[0]}"
    text = f"{round(value, 1):.1f}".rstrip("0").rstrip(".")
    return f"{text} {_LIMIT_UNITS[index]}"


@dataclass(frozen=True, slots=True)
class UploadSession:
    """The session as a caller sees it: what to send, where, and until when."""

    id: SessionId
    drive_id: DriveId
    parent_id: NodeId
    state: str
    declared_size: int
    part_size: int
    parts_total: int
    expires_at: datetime
    transfer_mode: str


@dataclass(frozen=True, slots=True)
class PartResult:
    """One part the session now holds.

    ``duplicate`` is what a resuming client sees for a part the server already
    accepted: the bytes were not re-stored and the counters did not move.
    """

    part_no: int
    size: int
    checksum: bytes
    duplicate: bool


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Where a resuming client should pick up."""

    id: SessionId
    state: str
    offset: int
    length: int
    parts_done: int
    parts_total: int
    accepted_parts: tuple[int, ...]
    expires_at: datetime

    @property
    def complete(self) -> bool:
        """Whether the accepted parts add up to the declared size.

        The byte count, not the part count, is the test: a client is free to
        send fewer, larger parts than ``parts_total`` suggests, and what
        ``complete`` promises is that there is nothing left to send.
        """
        return self.parts_done > 0 and self.offset == self.length


@dataclass(frozen=True, slots=True)
class _SessionRow:
    """The session columns the service reasons about.

    Read as columns rather than as the mapped entity: whole-entity reads of a
    Files model belong to ``FilesRepo``, and a projection also cannot hand back
    a stale instance from the identity map after a compare-and-swap.
    """

    id: uuid.UUID
    drive_id: uuid.UUID
    parent_id: uuid.UUID
    state: str
    declared_size: int
    bytes_received: int
    expires_at: datetime
    transfer_mode: str
    #: The folder lease epoch the session was admitted under, or ``0`` for a
    #: session opened on an unleased folder. The commit runs long after the
    #: request that opened it — in a worker, with no headers — so this is the
    #: only place the fence it already passed survives.
    lease_epoch: int
    #: Who held that lease, as the lease row records its holder. The other
    #: thing the fence compares, and the one the commit cannot re-derive: the
    #: worker that promotes acts as the platform janitor, so the principal
    #: behind the commit is nobody's holder.
    lease_holder: uuid.UUID | None
    #: Which id space ``lease_holder`` is drawn from — recorded beside it so
    #: the commit re-presents the whole pair the fence compares.
    lease_holder_kind: str | None
    #: The holder's displaced bytes, filed as a conflicted copy the drive
    #: names (see :class:`ConflictSubmission`).
    conflict_copy: bool = False
    #: The node those bytes displaced, when the holder named one.
    conflict_of: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class _PartRow:
    """The accepted part, as the idempotency decision needs it."""

    part_no: int
    size: int
    checksum: bytes


class UploadService:
    """Open sessions, accept their parts, report progress, abort them."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore,
        *,
        part_size: int = PART_SIZE,
        checkpoints: Checkpoints = _NO_CHECKPOINTS,
        ceilings: CeilingsResolver | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        if part_size <= 0:
            raise ValueError("part_size must be positive")
        self._part_size = part_size
        self._checkpoints = checkpoints
        self._quota = QuotaService(
            repo, ctx, clock, store, checkpoints=checkpoints, ceilings=ceilings
        )

    # ---- open ------------------------------------------------------------

    async def open(
        self,
        drive_id: DriveId,
        parent_id: NodeId,
        name: bytes,
        *,
        declared_size: int,
        mime_hint: str | None = None,
        transfer_mode: str = "proxied",
        lease: LeaseContext | None = None,
        conflict_of: NodeId | None = None,
        conflict_copy: bool = False,
    ) -> UploadSession:
        """Reserve room and a session id, before a single byte is accepted.

        ``conflict_copy`` (implied by ``conflict_of``) opens the holder's
        *conflict submission*: bytes its disk held until a write from the web
        displaced them. They land as a conflicted copy the drive names, beside
        ``conflict_of`` when the holder knows which node they were. Only the
        fenced holder of the lease covering the folder may open one; anyone
        else is refused ``files.lease_mismatch`` before anything is reserved.

        The quota decision and the session row are one transaction, which is
        what makes ``Σ holds == Σ live sessions`` true by construction: there is
        no instant where the row exists without its hold or the hold without
        its row.

        The folder lease is decided **here**, at the only moment the caller's
        epoch is in hand: a session that will create a node inside somebody
        else's mount is refused before a byte is staged, and one that is
        admitted records the epoch it was admitted under, because the commit
        that finishes it runs later and elsewhere with no headers to present.

        The drive is taken ``FOR UPDATE`` here rather than left to the quota
        reserve below, for the reason :meth:`open_single` spells out: inserting
        the session row takes ``FOR KEY SHARE`` on the drive for the foreign
        key, and that lock is shared, so two openers both held it and both then
        had to upgrade the same row for their quota decision — each waiting on
        the other's share, which Postgres answers by killing one as a deadlock.
        A browser uploading a folder opens dozens of sessions at once, so that
        collision is the common case and every loser was a file dropped on the
        floor. Locking first makes this the same drive → parent → node order
        every other writer uses.
        """
        try:
            names.validate(name)
        except names.InvalidName as exc:
            raise InvalidRequest(f"files.invalid_name.{exc.code}", str(exc)) from exc
        if declared_size < 0:
            raise InvalidRequest("files.invalid_size", "declared size may not be negative")
        self._refuse_when_too_large(declared_size)
        submission = conflict_copy or conflict_of is not None
        if submission and (lease is None or lease.epoch is None):
            raise _not_the_holder()
        async with self._repo.transaction():
            drive = await self._repo.lock_drive(drive_id)
            if drive is None:
                raise NotFound(message=f"drive {drive_id}")
            parent = await self._repo.node(parent_id)
            if parent is None or parent.drive_id != drive.id:
                raise NotFound(message=f"folder {parent_id}")
            if parent.kind != "folder":
                raise InvalidRequest("files.not_a_folder", f"{parent_id} is not a folder")
            if submission:
                await self._admit_submission(parent, lease, conflict_of)
            # An upload aimed at a chat is aimed at the conversation, and lands
            # in the working directory the agent reads — the one place a lease
            # that takes inbound writes opens. Only an unfenced upload is
            # steered: the box handing a chat back writes its records at the
            # chat's top level under its epoch, and lands exactly where it aims.
            if lease is None or lease.epoch is None:
                parent = await self._drop_target(parent)
            # Refused at open rather than at complete: a session the caller has
            # already pushed bytes into is the worst place to learn the folder
            # was never going to take the file.
            assert_traversal_rules(parent)
            covering = await fenced_write_for(self._repo, parent, lease, into=True)
            # The holder this session is opened under, or nothing: only a write
            # that PRESENTED an epoch is a hand-back, and a write the drive
            # merely admitted into an awake chat records no holder, so its
            # commit has to be admitted again rather than land as the holder's.
            recorded_holder = (
                lease.holder
                if lease is not None and covering is not None and is_hand_back(covering, lease)
                else None
            )
            await self._refuse_when_too_many_open()
            session = FileUploadSession(
                org_team_id=self._repo.scope.org_team_id,
                drive_id=drive.id,
                parent_id=parent.id,
                name=name,
                dedup_domain_id=drive.dedup_domain_id,
                state="open",
                declared_size=declared_size,
                transfer_mode=transfer_mode,
                # Only a caller that PRESENTED an epoch gets one recorded: the
                # commit re-presents it, and a write the drive merely admitted
                # into an awake chat must be admitted again rather than land as
                # though it held the lease.
                lease_epoch=(
                    int(covering.epoch)
                    if covering is not None and is_hand_back(covering, lease)
                    else 0
                ),
                # …and by whom. The epoch alone would let the commit finish
                # under a lease this caller never held. The kind rides with the
                # id: the fence compares the pair, so half of it recorded here
                # would be matched against a holder of the other kind.
                lease_holder=None if recorded_holder is None else recorded_holder.id,
                lease_holder_kind=None if recorded_holder is None else recorded_holder.kind,
                conflict_copy=submission,
                conflict_of=conflict_of,
                expires_at=_deadline(),
                created_by=self._actor(),
            )
            await self._repo.add(session)
            await self._repo.flush()
            # The push that accompanies the release lands whatever the ceilings
            # say (see QuotaService): its bytes are already on the box, and a
            # refusal would lose them. Every other fenced write — the live plane
            # between the checkpoints included — is bounded like anyone else's.
            await self._quota.reserve_on_session(
                session,
                parent_path=parent.path_ids,
                unbounded=is_final_push(covering, lease),
            )
            row = await self._session_row(SessionId(session.id))
        _ = mime_hint  # sniffed from the bytes at commit time, never trusted here
        return self._as_session(row)

    async def _admit_submission(
        self, parent: FileNode, lease: LeaseContext | None, conflict_of: NodeId | None
    ) -> None:
        """Refuse a conflict submission from anyone but the folder's fenced holder.

        Every refusal the fence can give — no lease here, a superseded epoch,
        another machine's instance — is the one answer: the caller is not the
        holder this kind of session belongs to. The displaced node, when named,
        must be a live file in this very folder: the copy is filed beside it.
        """
        try:
            covering = await fenced_write_for(self._repo, parent, lease, into=True)
        except Conflict as exc:
            raise _not_the_holder() from exc
        if not is_hand_back(covering, lease):
            raise _not_the_holder()
        if conflict_of is None:
            return
        displaced = await self._repo.node(conflict_of)
        if displaced is None or displaced.trashed_at is not None or displaced.kind != "file":
            raise NotFound(message=f"file {conflict_of}")
        if displaced.parent_id != parent.id:
            raise InvalidRequest(
                "files.conflict_of_elsewhere", "the displaced file is not in that folder"
            )

    async def open_single(
        self,
        drive_id: DriveId,
        parent_id: uuid.UUID,
        name: bytes,
        *,
        node_id: NodeId,
        declared_size: int,
        session_id: SessionId | None = None,
    ) -> UploadSession:
        """Open the session a single-call content PUT stages its object under,
        holding no room yet.

        The object under ``incoming/<session>/`` belongs to this row, and the
        reconciliation worker deletes it when the row expires. ``session_id``
        is the id the caller already staged the bytes under, so the row owns
        them; a staged object whose row was never opened has no owner and is
        swept once it is past the grace period. The node is already known here
        — a single-call PUT replaces the content of an existing file — so the
        name is the node's own and no name check is repeated.

        The drive row is not locked: the row's foreign key takes ``FOR KEY
        SHARE`` on it, which every other writer's no-key lock admits, so the
        caller may go on to the store before it takes the drive at all. The
        room is taken by :meth:`reserve_single`, under the drive row, once the
        store calls are done.
        """
        if declared_size < 0:
            raise InvalidRequest("files.invalid_size", "declared size may not be negative")
        async with self._repo.transaction():
            drive = await self._repo.drive(drive_id)
            if drive is None:
                raise NotFound(message=f"drive {drive_id}")
            session = FileUploadSession(
                id=session_id or uuid.uuid4(),
                org_team_id=self._repo.scope.org_team_id,
                drive_id=drive.id,
                node_id=node_id,
                parent_id=parent_id,
                name=name,
                dedup_domain_id=drive.dedup_domain_id,
                state="uploading",
                declared_size=declared_size,
                transfer_mode="single",
                expires_at=_deadline(),
                created_by=self._actor(),
            )
            await self._repo.add(session)
            await self._repo.flush()
            row = await self._session_row(SessionId(session.id))
        return self._as_session(row)

    async def reserve_single(
        self,
        session_id: SessionId,
        *,
        drive_id: DriveId,
        declared_size: int,
        parent_path: str | None = None,
        unbounded: bool = False,
    ) -> None:
        """Take the room a single-call session's bytes need, or refuse.

        Under the drive row (the quota reserve takes it first), so two writers
        see each other's holds and the count of open sessions; the session
        being reserved is already open and is not counted against itself.
        """
        async with self._repo.transaction():
            hold = await self._quota.reserve(
                drive_id,
                bytes=declared_size,
                nodes=1,
                session_id=session_id,
                parent_path=parent_path,
                unbounded=unbounded,
            )
            await self._refuse_when_too_many_open(besides=session_id)
            await self._repo.session.execute(
                update(_SESSIONS)
                .where(
                    FileUploadSession.id == session_id,
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                )
                .values(quota_hold_bytes=hold.bytes, quota_hold_nodes=hold.nodes)
            )

    async def _drop_target(self, parent: FileNode) -> FileNode:
        """The folder an upload aimed at ``parent`` really lands in.

        Every destination but an object's folder is itself; a chat answers with
        its working directory, minted on the way if the chat predates it. The
        same rule a move follows, resolved here before the destination is
        fenced or counted so the session names the folder actually written.
        """
        # Imported here: the bridge builds a Namespace of its own to mint a
        # chat's folders, so naming it at module scope would be a cycle.
        from alkera_core.files.objects_bridge import drop_target_for

        namespace = Namespace(
            self._repo, self._ctx, self._clock, self._store, checkpoints=self._checkpoints
        )
        return await drop_target_for(self._repo, self._ctx, namespace, parent)

    def _refuse_when_too_large(self, declared_size: int) -> None:
        """Refuse a size this deployment will never be able to commit.

        Both ceilings are decided here, at open, because the alternative is the
        thing this exists to prevent: a caller spends a whole transfer and then
        meets the refusal at the commit, with nothing in the answer saying how
        big a file it could have sent instead.

        The file ceiling is the one a reader is meant to meet, so it is said in
        the reader's own words and units. The part ceiling behind it is an
        operator's contradiction, not a reader's: the settings refuse a part
        size that cannot carry the published file ceiling
        (``_validate_files_upload_ceiling``), so a correctly configured
        deployment can only reach it by cutting a session into smaller parts
        than it published, and the message says exactly that.
        """
        maximum = settings.files_max_file_bytes
        if declared_size > maximum:
            raise TooLarge(
                "files.too_large",
                f"exceeded the maximum upload size of {max_upload_size_label(maximum)}",
            )
        parts = self._parts_total(declared_size)
        max_parts = settings.files_max_upload_parts
        if parts > max_parts:
            raise TooLarge(
                "files.too_large",
                f"declared size {declared_size} bytes needs {parts} parts of "
                f"{self._part_size} bytes, over this deployment's limit of {max_parts}",
            )

    async def _refuse_when_too_many_open(self, *, besides: SessionId | None = None) -> None:
        statement = (
            select(func.count())
            .select_from(FileUploadSession)
            .where(FileUploadSession.state.in_(UPLOAD_SESSION_LIVE_STATES))
        )
        if besides is not None:
            statement = statement.where(FileUploadSession.id != besides)
        live = int((await self._repo.execute_scoped(statement)).scalar_one())
        if live >= settings.files_max_open_sessions_per_org:
            raise Conflict(
                "files.too_many_sessions",
                f"the org already holds {live} live upload sessions",
            )

    # ---- parts -----------------------------------------------------------

    async def put_part(
        self,
        session_id: SessionId,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        """Stream one part into the session, verifying it while it streams.

        Three steps, and only the first and last touch the database. The claim
        checks the session and the part number and commits; the bytes then
        stream into the store with no transaction open, so neither the session
        row nor a pooled connection is held for as long as a client takes to
        send a part, and the parts of one upload stream side by side; the row
        is written last, in a short transaction of its own.

        The store write happens before the row so a crash between them leaves
        an object the session still owns (the sweeper deletes it) rather than a
        row promising bytes that were never written.

        Emptiness is decided against the session, not against the part alone: a
        zero-byte file is one zero-byte part (``parts_total(0) == 1``), and it
        is the only way to create one through this API — so the refusal has to
        know what the session declared, which means it happens after the claim.
        The pairing is exact in both directions: a session that declared bytes
        cannot be satisfied with none, and one that declared none cannot smuggle
        bytes past the quota it reserved.
        """
        if size < 0:
            raise InvalidRequest("files.empty_part", "a part may not declare negative bytes")
        async with self._repo.transaction():
            existing = await self._claim_part(session_id, part_no, size=size)
        if existing is not None:
            self._refuse_mismatch(existing, checksum, part_no)
            return PartResult(
                part_no=part_no, size=existing.size, checksum=existing.checksum, duplicate=True
            )
        key = part_key(session_id, part_no, checksum)
        await self._stage(key, part_no, data, size=size, checksum=checksum)
        await self._checkpoints.reach("uploads.after_part_stored")
        await self._checkpoints.reach("uploads.before_part_row")
        try:
            async with self._repo.transaction():
                return await self._record_part(session_id, part_no, size=size, checksum=checksum)
        except Conflict:
            # Nothing names these bytes: a different part won the number (its
            # checksum, so its key, differs) or the session stopped accepting
            # parts. A failed delete is the sweeper's to finish.
            with contextlib.suppress(StoreError):
                await self._store.delete(key)
            raise

    async def _claim_part(
        self, session_id: SessionId, part_no: int, *, size: int
    ) -> _PartRow | None:
        """Claim the session for one part, and return the part if it already landed."""
        claimed = await self._claim(session_id)
        if size == 0 and claimed.declared_size != 0:
            raise InvalidRequest("files.empty_part", "a part may not be empty")
        if size != 0 and claimed.declared_size == 0:
            raise InvalidRequest(
                "files.size_mismatch",
                f"the session declared zero bytes; part {part_no} declared {size}",
            )
        if part_no < 1 or part_no > self._parts_total(claimed.declared_size):
            raise InvalidRequest(
                "files.part_out_of_range",
                f"part {part_no} is outside 1..{self._parts_total(claimed.declared_size)}",
            )
        return await self._part(session_id, part_no)

    async def _stage(
        self, key: str, part_no: int, data: AsyncIterator[bytes], *, size: int, checksum: bytes
    ) -> None:
        """Stream the part's bytes to ``key``, refusing in the Files vocabulary."""
        try:
            await self._store.put(key, data, size=size, checksum=checksum, if_absent=False)
        except ChecksumMismatch as exc:
            # The bytes disagree with the checksum the client declared for
            # them. That is the caller's error, and it leaves here in the
            # Files vocabulary — a driver exception escaping the library
            # would reach the platform's catch-all as a 500.
            raise InvalidRequest(
                "files.part_checksum_mismatch",
                f"part {part_no} does not match the checksum it declared",
            ) from exc
        except StoreInvalidRequest as exc:
            # The driver refuses a stream that does not match the length it
            # was promised: a part that stopped short.
            raise InvalidRequest(
                "files.size_mismatch",
                f"part {part_no} declared {size} bytes and streamed fewer",
            ) from exc
        except (Throttled, Unavailable) as exc:
            raise StoreUnavailable(f"the object store refused part {part_no}") from exc
        except StoreError as exc:
            # Every remaining normalized class — an expired scoped
            # credential, a denied bucket, a bucket that was never created
            # — is a deployment fault the caller can do nothing about. It
            # still leaves in the Files vocabulary: a driver exception that
            # reaches the platform catch-all is an opaque 500 a client
            # retries blindly and an operator cannot tell from a bug.
            raise StoreUnavailable(f"the object store refused part {part_no}") from exc

    async def _record_part(
        self, session_id: SessionId, part_no: int, *, size: int, checksum: bytes
    ) -> PartResult:
        """Write the part's row and count its bytes, or defer to the row that won."""
        inserted = await self._insert_part(session_id, part_no, size=size, checksum=checksum)
        if not inserted:
            # Another request won the same part number; its row is the truth,
            # and it is only interchangeable with ours if the client sent the
            # same bytes.
            won = await self._part(session_id, part_no)
            if won is None:  # pragma: no cover — the row cannot vanish mid-transaction
                raise Conflict("files.part_mismatch", f"part {part_no} disappeared")
            self._refuse_mismatch(won, checksum, part_no)
            return PartResult(part_no=part_no, size=won.size, checksum=won.checksum, duplicate=True)
        if not await self._count_part(session_id, size=size):
            # An abort or a commit landed while the part streamed: the state
            # machine, not arrival order, decides — and the row rolls back.
            raise Conflict(
                "files.session_state",
                f"session {session_id} stopped accepting parts mid-upload",
            )
        return PartResult(part_no=part_no, size=size, checksum=checksum, duplicate=False)

    @staticmethod
    def _refuse_mismatch(existing: _PartRow, checksum: bytes, part_no: int) -> None:
        if existing.checksum != checksum:
            raise Conflict(
                "files.part_mismatch",
                f"part {part_no} was already accepted with a different checksum",
            )

    async def _claim(self, session_id: SessionId) -> _SessionRow:
        """Move the session to ``uploading`` in one compare-and-swap."""
        statement = (
            update(_SESSIONS)
            .where(
                FileUploadSession.id == session_id,
                FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                FileUploadSession.state.in_(PART_ACCEPTING_STATES),
            )
            .values(state="uploading")
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        if int(result.rowcount) == 0:
            row = await self._maybe_session_row(session_id)
            if row is None:
                raise NotFound(message=f"upload session {session_id}")
            raise Conflict(
                "files.session_state",
                f"session {session_id} is {row.state} and accepts no parts",
            )
        return await self._session_row(session_id)

    async def _insert_part(
        self, session_id: SessionId, part_no: int, *, size: int, checksum: bytes
    ) -> bool:
        return await self._repo.insert_upload_part(
            session_id=session_id, part_no=part_no, size=size, checksum=checksum
        )

    async def _count_part(self, session_id: SessionId, *, size: int) -> bool:
        """Count the bytes and push the deadline out, in one statement.

        The ``state`` predicate is what an abort racing this part loses or wins
        on: if the session left ``uploading`` in between, no row is updated and
        the caller rolls the part back.
        """
        statement = (
            update(_SESSIONS)
            .where(
                FileUploadSession.id == session_id,
                FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                FileUploadSession.state == "uploading",
            )
            .values(
                bytes_received=FileUploadSession.bytes_received + size,
                expires_at=_deadline(),
            )
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        return int(result.rowcount) == 1

    # ---- status ----------------------------------------------------------

    async def status(self, session_id: SessionId) -> SessionStatus:
        """What a resuming client needs: the offset and exactly which parts landed."""
        async with self._repo.transaction():
            row = await self._session_row(session_id)
            accepted = await self._accepted_parts(session_id)
        return SessionStatus(
            id=SessionId(row.id),
            state=row.state,
            offset=row.bytes_received,
            length=row.declared_size,
            parts_done=len(accepted),
            parts_total=self._parts_total(row.declared_size),
            accepted_parts=accepted,
            expires_at=row.expires_at,
        )

    # ---- abort -----------------------------------------------------------

    async def abort(self, session_id: SessionId) -> None:
        """End a live session and give its room back in the same transaction.

        The staged objects are deleted afterwards and best-effort: the row is
        what decides whether the session is over, and the reconciliation
        sweeper finishes any object the store refused to drop.
        """
        async with self._repo.transaction():
            statement = (
                update(_SESSIONS)
                .where(
                    FileUploadSession.id == session_id,
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                    FileUploadSession.state.in_(UPLOAD_SESSION_LIVE_STATES),
                )
                .values(state="aborted")
            )
            result = cast(CursorResult[Any], await self._repo.session.execute(statement))
            if int(result.rowcount) == 0:
                row = await self._maybe_session_row(session_id)
                if row is None:
                    raise NotFound(message=f"upload session {session_id}")
                raise Conflict(
                    "files.session_state", f"session {session_id} is already {row.state}"
                )
            await self._quota.release(session_id)
            staged = await _accepted_part_rows(self._repo, session_id)
        await _delete_staged(self._store, session_id, staged)

    # ---- internals -------------------------------------------------------

    def _actor(self) -> uuid.UUID:
        """Who opened this session — the human behind an agent, else the caller,
        folded so a token-named principal is recorded rather than dropped."""
        return subject_ref(self._ctx)

    async def _session_row(self, session_id: SessionId) -> _SessionRow:
        row = await self._maybe_session_row(session_id)
        if row is None:
            raise NotFound(message=f"upload session {session_id}")
        return row

    async def _maybe_session_row(self, session_id: SessionId) -> _SessionRow | None:
        """The row as Postgres holds it now, not as the identity map cached it."""
        statement = select(
            FileUploadSession.id,
            FileUploadSession.drive_id,
            FileUploadSession.parent_id,
            FileUploadSession.state,
            FileUploadSession.declared_size,
            FileUploadSession.bytes_received,
            FileUploadSession.expires_at,
            FileUploadSession.transfer_mode,
            FileUploadSession.lease_epoch,
            FileUploadSession.lease_holder,
            FileUploadSession.lease_holder_kind,
            FileUploadSession.conflict_copy,
            FileUploadSession.conflict_of,
        ).where(FileUploadSession.id == session_id)
        row = (await self._repo.execute_scoped(statement)).one_or_none()
        if row is None:
            return None
        return _SessionRow(*row)

    async def _part(self, session_id: SessionId, part_no: int) -> _PartRow | None:
        statement = select(
            FileUploadPart.part_no, FileUploadPart.size, FileUploadPart.checksum
        ).where(FileUploadPart.session_id == session_id, FileUploadPart.part_no == part_no)
        row = (await self._repo.execute_scoped(statement)).one_or_none()
        return None if row is None else _PartRow(*row)

    async def _accepted_parts(self, session_id: SessionId) -> tuple[int, ...]:
        statement = (
            select(FileUploadPart.part_no)
            .where(FileUploadPart.session_id == session_id)
            .order_by(FileUploadPart.part_no)
        )
        rows = (await self._repo.execute_scoped(statement)).scalars().all()
        return tuple(int(part_no) for part_no in rows)

    def _parts_total(self, declared_size: int) -> int:
        return parts_total(declared_size, self._part_size)

    def _as_session(self, row: _SessionRow) -> UploadSession:
        return UploadSession(
            id=SessionId(row.id),
            drive_id=DriveId(row.drive_id),
            parent_id=NodeId(row.parent_id),
            state=row.state,
            declared_size=row.declared_size,
            part_size=self._part_size,
            parts_total=self._parts_total(row.declared_size),
            expires_at=row.expires_at,
            transfer_mode=row.transfer_mode,
        )


def _not_the_holder() -> Conflict:
    return Conflict(
        "files.lease_mismatch", "only the machine holding this folder files a conflicted copy"
    )


# ---- the commit half -----------------------------------------------------

#: A ``committing`` session whose operation is this old and not running is
#: stuck: the worker that owned it is gone.
COMMITTING_DEADLINE: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ConflictSubmission:
    """Where the holder's displaced bytes were filed, answered by ``complete``.

    The name is the drive's — the holder renames its staged file to it — and
    is decided when the session completes, so it is known before the bytes
    land. ``node_id``/``name`` are ``None`` when the bytes are kept only as a
    version of the node they displaced (a machine-managed path, or a drive
    with no room for another file): there is no copy for the holder to name.
    ``conflict_id`` is ``None`` when the holder named no node, because a
    conflict row links a copy to the node it came from.
    """

    node_id: NodeId | None
    name: bytes | None
    conflict_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class PartRef:
    """One part as the completing client believes the server holds it."""

    part_no: int
    size: int
    checksum: bytes


class UploadCompletion:
    """The commit half of an upload: ``complete``, ``promote``, the sweeper.

    ``complete`` never touches the bytes. It agrees with the client on which
    parts exist, closes the session to writers and hands back a queued
    operation — so completing a 20-part upload and completing one whose bytes
    the org already holds cost the caller the same wait, and nothing a client
    can time says whether the content was already there.

    ``promote`` is the activity body a worker runs. It re-runs the
    authorization facts and re-reads the target *before* a byte moves, because
    an upload that opened an hour ago may be landing in a folder the caller has
    since lost or one that has been trashed. A refusal releases the hold and
    leaves the staged objects for the sweeper rather than deleting bytes a
    retry may still want.
    """

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore,
        *,
        part_size: int = PART_SIZE,
        checkpoints: Checkpoints = _NO_CHECKPOINTS,
        reauthorize: Reauthorize | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._part_size = part_size
        self._checkpoints = checkpoints
        self._reauthorize = reauthorize
        self._quota = QuotaService(repo, ctx, clock, store, checkpoints=checkpoints)
        self._ops = Operations(repo, ctx, clock, store, checkpoints=checkpoints)

    # ---- complete --------------------------------------------------------

    async def complete(
        self,
        session_id: SessionId,
        parts: Sequence[PartRef],
        *,
        conflict: CommitBehavior = "fail",
        if_match: int | None = None,
    ) -> OperationState:
        """Agree on the parts, close the session to writers, queue the commit.

        The compare-and-swap to ``committing``, the part agreement and the
        operation row are one transaction, which is what makes a refusal
        self-healing: raising rolls the state back to ``uploading`` with no
        second statement, so a client that sent a stale list can fix it and
        complete again.

        The store is asked about the parts first, before the session row is
        claimed: one ``head`` per part, for a sixteen-part upload sixteen calls
        to somebody else's server, none of which may hold a row. The claim then
        checks that the parts it closes the session on are the parts the store
        was asked about; a part that landed in between refuses the completion,
        to be sent again.
        """
        async with self._repo.transaction():
            await self._refuse_unless_completable(session_id)
            proved = await self._accepted(session_id)
        try:
            _agree(parts, proved)
            await self._agree_with_store(session_id, proved)
        except Conflict:
            # A session aborted or completed meanwhile is refused for what
            # happened to it, not for the parts it no longer holds.
            async with self._repo.transaction():
                await self._refuse_unless_completable(session_id)
            raise
        async with self._repo.transaction():
            row = await self._claim_for_commit(session_id)
            if conflict == "fail" and not row.conflict_copy and await self._name_taken(row):
                # Answered on the call, where raising puts the session back in
                # ``uploading`` with every part still held: the caller's answer
                # (keep both, replace) is then a second completion of the same
                # bytes. Left to the queued commit, the refusal would release
                # the session and the answer would have to resend the file. The
                # commit still decides under its own lock; this only moves the
                # common case to where it costs nothing to answer.
                raise Conflict("files.exists", "that name is taken in this folder")
            accepted = await self._accepted(session_id)
            await self._checkpoints.reach("uploads.after_read_parts")
            _agree(parts, accepted)
            if accepted != proved:
                raise Conflict(
                    "files.parts_mismatch",
                    "a part landed while the upload was completing; complete it again",
                )
            await self._checkpoints.reach("uploads.after_parts_agreed")
            state = await self._ops.start(
                "upload", drive_id=DriveId(row.drive_id), total=len(accepted)
            )
            await self._bind(state.id, session_id=session_id, conflict=conflict, if_match=if_match)
            if row.conflict_copy:
                await self._reserve_submission(row, OperationId(state.id), session_id)
        return state

    async def submission(self, op_id: OperationId) -> ConflictSubmission | None:
        """What ``complete`` decided for a conflict submission, or ``None``."""
        document = (await self._ops.get(op_id)).result.get("conflict_submission")
        if not isinstance(document, dict):
            return None
        raw_copy = document.get("copy_node_id")
        raw_name = document.get("copy_name")
        raw_conflict = document.get("conflict_id")
        return ConflictSubmission(
            node_id=None if not raw_copy else NodeId(uuid.UUID(str(raw_copy))),
            name=None if not raw_name else bytes.fromhex(str(raw_name)),
            conflict_id=None if not raw_conflict else uuid.UUID(str(raw_conflict)),
        )

    async def _reserve_submission(
        self, row: _SessionRow, op_id: OperationId, session_id: SessionId
    ) -> None:
        """Name the conflicted copy now, so the holder learns it from ``complete``.

        The drive is the one namer, and it names under its own lock: the copy
        node is minted here, empty, and the operation carries where the bytes
        go. With a displaced node named, the bytes land on THAT node as a
        version beside its head (so its history holds them whatever happens to
        the copy), and the promote carries them onto the copy it reserved here.
        """
        from alkera_core.files import conflict_auto

        drive_id = DriveId(row.drive_id)
        await self._repo.lock_namespace(drive_id)
        parent = await self._repo.node(NodeId(row.parent_id))
        if parent is None or parent.trashed_at is not None:
            raise NotFound(message=f"folder {row.parent_id}")
        fence = await self._commit_fence(parent, row)
        covering = await covering_lease(self._repo, parent)
        if fence is None or covering is None:
            raise _not_the_holder()
        who = await conflict_auto.holder_name(self._repo, covering)
        conflict_id: uuid.UUID | None = None
        target: conflict_auto.CopyTarget | None = None
        if row.conflict_of is not None:
            _, _, node = await self._repo.lock_chain(drive_id, None, NodeId(row.conflict_of))
            if node is None or node.trashed_at is not None or node.kind != "file":
                raise NotFound(message=f"file {row.conflict_of}")
            target = await conflict_auto.reserve_copy(
                self._repo, self._ctx, self._clock, node=node, displaced_by=who, lease=fence
            )
            conflict_id = uuid.uuid4()
            landing = NodeId(node.id)
            copy_node_id, copy_name = target.node_id, target.name
        else:
            name = await self._name(session_id)
            taken = {
                bytes(sibling.name) for sibling in await self._repo.siblings(NodeId(parent.id))
            }
            created = await Namespace(
                self._repo, self._ctx, self._clock, self._store, checkpoints=self._checkpoints
            ).create(
                drive_id,
                NodeId(parent.id),
                "file",
                free_conflicted_copy_name(name, who, self._clock.now(), taken.__contains__),
                lease=fence,
            )
            landing = NodeId(created.id)
            copy_node_id, copy_name = landing, bytes(created.name)
        await self._repo.session.execute(
            text(
                "UPDATE file_ops SET result_node_id = :node, "
                "result = result || CAST(:submission AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "node": landing,
                "submission": json.dumps(
                    {
                        "conflict_submission": {
                            "of": None if row.conflict_of is None else str(row.conflict_of),
                            "who": who,
                            "conflict_id": None if conflict_id is None else str(conflict_id),
                            "copy_node_id": None if copy_node_id is None else str(copy_node_id),
                            "copy_name": None if copy_name is None else copy_name.hex(),
                            "target": None if target is None else target.to_wire(),
                        }
                    }
                ),
                "id": op_id,
                "org": self._repo.scope.org_team_id,
            },
        )

    async def _refuse_unless_completable(self, session_id: SessionId) -> None:
        """The refusal the claim gives a session that no longer takes parts,
        given before the claim is tried."""
        row = await _maybe_session_row(self._repo, session_id)
        if row is None:
            raise NotFound(message=f"upload session {session_id}")
        if row.state not in PART_ACCEPTING_STATES:
            raise Conflict(
                "files.session_state", f"session {session_id} is {row.state} and cannot complete"
            )

    async def _claim_for_commit(self, session_id: SessionId) -> _SessionRow:
        """``open|uploading → committing`` as one compare-and-swap.

        A part racing this loses on the same row: its own claim blocks on the
        lock, then re-reads a state that no longer accepts parts.
        """
        statement = (
            update(_SESSIONS)
            .where(
                FileUploadSession.id == session_id,
                FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                FileUploadSession.state.in_(PART_ACCEPTING_STATES),
            )
            .values(state="committing")
        )
        result = cast(CursorResult[Any], await self._repo.session.execute(statement))
        if int(result.rowcount) == 0:
            row = await _maybe_session_row(self._repo, session_id)
            if row is None:
                raise NotFound(message=f"upload session {session_id}")
            raise Conflict(
                "files.session_state", f"session {session_id} is {row.state} and cannot complete"
            )
        return await _session_row(self._repo, session_id)

    async def _accepted(self, session_id: SessionId) -> tuple[_PartRow, ...]:
        return await _accepted_part_rows(self._repo, session_id)

    async def _staged_key(self, session_id: SessionId, row: _PartRow) -> str | None:
        """The key ``row``'s bytes are staged under, or ``None`` when no object
        of the size it records is there."""
        for key in staged_part_keys(session_id, row.part_no, row.checksum):
            info = await self._store.head(key)
            if info is not None and info.size == row.size:
                return key
        return None

    async def _agree_with_store(self, session_id: SessionId, accepted: Sequence[_PartRow]) -> None:
        """Refuse when the bytes the rows promise are not in the store.

        A ``DomainStore`` deliberately has no listing, so the store's part list
        is read the only tenant-safe way there is — one ``head`` per accepted
        part, which also proves the object is the size its row claims.
        """
        if not accepted:
            raise Conflict("files.parts_mismatch", "the session holds no parts")
        for row in accepted:
            if await self._staged_key(session_id, row) is None:
                raise Conflict(
                    "files.parts_mismatch",
                    f"part {row.part_no} is not in the store as the session recorded it",
                )

    async def _bind(
        self,
        op_id: OperationId,
        *,
        session_id: SessionId,
        conflict: CommitBehavior,
        if_match: int | None = None,
    ) -> None:
        """Tie the operation to its session, its conflict rule and its precondition.

        The precondition rides the row rather than the request because the
        commit runs later and elsewhere: a ``replace`` agreed against etag 7
        must still refuse to land when the node moved on to 8 while the parts
        were in flight, and the queued commit has no headers of its own.
        """
        await self._repo.session.execute(
            text(
                "UPDATE file_ops SET result = result || CAST(:result AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "id": op_id,
                "org": self._repo.scope.org_team_id,
                "result": json.dumps(
                    {
                        OPERATION_SESSION_KEY: str(session_id),
                        "conflict": conflict,
                        "if_match": if_match,
                    }
                ),
            },
        )

    # ---- promote ---------------------------------------------------------

    async def promote_operation(self, op_id: OperationId) -> VersionInfo:
        """Promote the upload the operation ``op_id`` was queued for.

        ``complete`` binds the session onto the operation row in the same
        transaction that queues it (``result.session_id``), so the row alone
        names the work. Every runner -- the request's inline dispatch, the
        worker's workflow and the recovery of a lost hand-off -- starts a
        promote from ``(op_id, org)`` like every other queued kind.
        """

        async def body(progress: Progress) -> VersionInfo:
            # Inside the run, so a row that cannot be promoted ends ``failed``
            # with its reason rather than staying ``queued`` for the recovery
            # pass to offer again every tick.
            bound = session_of_operation(await self._ops.get(op_id))
            if bound is None:
                raise Conflict(
                    "files.session_state", f"operation {op_id} names no upload session to commit"
                )
            return await self._promote(bound, op_id, progress)

        return await self._run_promote(op_id, body)

    async def promote(self, session_id: SessionId, op_id: OperationId) -> VersionInfo:
        """Turn the staged parts into a version, under the operation ``op_id``.

        The name is chosen exactly once. A replay finds the node this operation
        already created on the row and writes onto it, which is what keeps a
        retried ``conflict="rename"`` from producing ``report (2).pdf`` beside
        the ``report (1).pdf`` the first attempt made.
        """
        return await self._run_promote(
            op_id, lambda progress: self._promote(session_id, op_id, progress)
        )

    async def _run_promote(
        self, op_id: OperationId, body: Callable[[Progress], Awaitable[VersionInfo]]
    ) -> VersionInfo:
        """Run a promote body under ``op_id``, which settles the row either way."""
        info = await self._ops.run(op_id, body)
        if info is None:  # pragma: no cover - promote is never cancellable mid-flight
            raise Conflict("files.session_state", f"operation {op_id} was cancelled")
        return info

    async def _promote(
        self, session_id: SessionId, op_id: OperationId, progress: Progress
    ) -> VersionInfo:
        from alkera_core.files.content import ContentService

        row = await self._require_committing(session_id)
        # What this operation owes, said before a byte moves. The commit was
        # answered 202, so the poll is the only place the caller can watch a
        # large upload land — and the watchdog reads the same figures to decide
        # how long silence is allowed to mean "still working" rather than
        # "dead". A terabyte given a flat ten minutes is failed mid-flight.
        await progress.declare_bytes(row.bytes_received)
        try:
            node_id = await self._land(session_id, op_id, row)
        except FilesError:
            await self._refuse(session_id)
            raise
        await self._checkpoints.reach("uploads.before_promote_bytes")
        content = ContentService(
            self._repo, self._ctx, self._clock, self._store, checkpoints=self._checkpoints
        )
        async with self._repo.transaction():
            node = await self._repo.node(node_id)
            if node is None:  # pragma: no cover - the node this op made cannot vanish
                raise NotFound(message=f"file {node_id}")
            # The version is fenced against the etag ``_land`` agreed the commit
            # against, never against whatever the node's etag is by now: the
            # adoption commits before the bytes are written, so reading it fresh
            # here would let a writer who landed a version in that window be
            # clobbered with no 412. ``None`` means this operation created the
            # node itself, so there is no prior version to fence.
            agreed = await self._if_match(op_id)
            etag = node.etag if agreed is None else agreed
            fence = await self._commit_fence(node, row)
            submitted = (await self._ops.get(op_id)).result.get("conflict_submission")
        # A submission that named the node it displaced parks the bytes on that
        # node, beside its head; one that named none lands them on the fresh
        # copy ``complete`` minted, like any upload.
        parked = isinstance(submitted, dict) and submitted.get("of") is not None
        info = await content.put_version(
            node_id,
            self._counted(self._concatenated(session_id), progress),
            size_declared=row.bytes_received,
            if_match=etag,
            lease=fence,
            park=parked,
        )
        async with self._repo.transaction():
            if parked and isinstance(submitted, dict):
                await self._settle_submission(node_id, info, submitted)
            await self._quota.release(session_id)
            # What landed, recorded on the operation itself: the commit was
            # answered 202 long before this ran, so the poll is the only thing
            # that can tell the caller which version their bytes became — and
            # whether the drive already held them.
            await self._repo.session.execute(
                text(
                    "UPDATE file_ops SET result = result || CAST(:landed AS jsonb) "
                    "WHERE id = :id AND org_team_id = :org"
                ),
                {
                    "landed": json.dumps({"version_id": str(info.id), "unchanged": info.unchanged}),
                    "id": op_id,
                    "org": self._repo.scope.org_team_id,
                },
            )
            await self._repo.session.execute(
                update(_SESSIONS)
                .where(
                    FileUploadSession.id == session_id,
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                )
                .values(state="done")
            )
        await self._drop_parts(session_id)
        return info

    async def _settle_submission(
        self, node_id: NodeId, info: VersionInfo, submitted: dict[str, Any]
    ) -> None:
        """Put the parked submission where ``complete`` said, and on record.

        In the transaction that marks the session done, so a promote retried
        after a crash settles once: a replay parks the bytes again (the same
        bytes, one more version) but finds the session no longer committing
        only after this has committed with it.
        """
        from alkera_core.files import conflict_auto

        located = await self._repo.node(node_id)
        if located is None:  # pragma: no cover - the node the bytes just landed on
            raise NotFound(message=f"file {node_id}")
        _, _, node = await self._repo.lock_chain(DriveId(located.drive_id), None, node_id)
        parked = await self._repo.version(VersionId(info.id))
        if node is None or parked is None:  # pragma: no cover - both were just written
            raise NotFound(message=f"file {node_id}")
        head = (
            None
            if node.head_version_id is None
            else await self._repo.version(VersionId(node.head_version_id))
        )
        raw_target = submitted.get("target")
        raw_conflict = submitted.get("conflict_id")
        await conflict_auto.settle_displacement(
            self._repo,
            self._ctx,
            self._clock,
            node=node,
            displaced=parked,
            kept=head,
            base=head,
            target=conflict_auto.CopyTarget.from_wire(
                raw_target if isinstance(raw_target, dict) else {}
            ),
            arrived_from="web",
            kept_by=await conflict_auto.writer_name(self._repo, head),
            displaced_by=str(submitted.get("who") or conflict_auto.ANONYMOUS_WRITER),
            conflict_id=None if not raw_conflict else uuid.UUID(str(raw_conflict)),
        )

    async def _commit_fence(self, node: FileNode, row: _SessionRow) -> LeaseContext | None:
        """The lease context the queued commit finishes under.

        The commit is the second half of a request the fence already admitted,
        and it has no headers of its own — so the epoch and the holder come off
        the session row, written there when the open was fenced, and the
        instance off the lease that is live *now*. All three are what the fence
        compares: a lease that has since changed hands has a different epoch,
        so the commit is refused rather than landing bytes in somebody else's
        mount.

        The holder is emphatically **not** the principal behind the commit.
        ``promote`` runs in the Temporal worker, which acts for the platform as
        the janitor, and inline it runs under whoever called ``complete`` —
        neither is the person whose lease admitted the open, so fencing on the
        caller would refuse the holder their own file. What the session row
        carries is what the fence already proved once, in the request that had
        the headers.

        ``None`` when nothing leases the folder, which is the ordinary upload.

        The leased folder is locked before the lease row is read at update
        strength: the fixed order puts it first, and both callers go on to lock
        nodes beneath it.
        """
        await self._repo.lock_lease_gate(NodeId(node.id))
        covering = await covering_lease(self._repo, node)
        if covering is None:
            return None
        if row.lease_epoch == 0:
            # The open presented no epoch of its own, so neither does the
            # commit: the fence is asked the same question again — which
            # refuses an ordinary upload into a folder leased while the parts
            # were in flight, and admits one the awake chat takes inbound.
            return None
        return LeaseContext(
            epoch=row.lease_epoch,
            instance_id=covering.holder_instance_id,
            # A session opened before the holder was recorded carries none, and
            # a context with no holder is refused: the fence never guesses.
            holder=(
                HolderIdentity(kind=cast(HolderKind, row.lease_holder_kind), id=row.lease_holder)
                if row.lease_holder is not None and row.lease_holder_kind is not None
                else None
            ),
        )

    async def _require_committing(self, session_id: SessionId) -> _SessionRow:
        async with self._repo.transaction():
            row = await _session_row(self._repo, session_id)
        if row.state != "committing":
            raise Conflict(
                "files.session_state", f"session {session_id} is {row.state}, not committing"
            )
        return row

    async def _land(self, session_id: SessionId, op_id: OperationId, row: _SessionRow) -> NodeId:
        """Re-authorize, re-read the target, and create the node exactly once."""
        state = await self._ops.get(op_id)
        if state.result_node_id is not None:
            return NodeId(state.result_node_id)
        if self._reauthorize is not None:
            await self._reauthorize(NodeId(row.parent_id))
        await self._checkpoints.reach("uploads.after_reauthorize")
        async with self._repo.transaction():
            parent = await self._repo.node(NodeId(row.parent_id))
            if parent is None or parent.kind != "folder" or parent.trashed_at is not None:
                raise NotFound(message=f"folder {row.parent_id}")
            if parent.drive_id != row.drive_id:
                raise NotFound(message=f"folder {row.parent_id}")
            # A lease taken while the parts were in flight fences the create,
            # so the folder never grows a node whose bytes the commit is about
            # to be refused. The same context goes on to the create itself,
            # which fences again inside its own lock: the commit has no headers
            # of its own, so this is the only place the epoch it was admitted
            # under can reach the namespace. The namespace lock comes before
            # this fence because the create takes it next, and the fixed order
            # puts it ahead of the leased folder and the lease row the fence
            # locks.
            await self._repo.lock_namespace(DriveId(row.drive_id))
            fence = await self._commit_fence(parent, row)
            admission = admitted_inbound(
                await fenced_write_for(self._repo, parent, fence, into=True)
            )
            namespace = Namespace(
                self._repo, self._ctx, self._clock, self._store, checkpoints=self._checkpoints
            )
            name = await self._name(session_id)
            conflict = await self._conflict(op_id)
            landed = None
            agreed: int | None = None
            if conflict == "replace":
                adopted = await self._adopt(
                    NodeId(row.parent_id), name, op_id, holder=row.lease_epoch != 0
                )
                if adopted is not None:
                    landed, agreed = adopted
            if landed is None:
                # ``replace`` with nothing to replace is a plain create, and the
                # namespace has no such behaviour: a name nobody holds can only
                # be taken outright.
                created = await namespace.create(
                    DriveId(row.drive_id),
                    NodeId(row.parent_id),
                    "file",
                    name,
                    conflict="fail" if conflict == "replace" else conflict,
                    lease=fence,
                )
                landed = NodeId(created.id)
            elif admission is not None:
                # Replacing a file that is already there does not go through the
                # namespace, so this is the one landing whose difference nothing
                # else records for the machine holding the chat. A create records
                # its own.
                adopted_node = await self._repo.node(landed)
                if adopted_node is not None:
                    await LiveEntriesService(self._repo, self._ctx).accept_inbound(
                        adopted_node, kind="inbound"
                    )
            # The etag the version must be fenced against rides the row beside
            # the node, so a promote retried after a crash fences the same way
            # the first attempt would have. A create writes ``null``: the
            # caller's precondition, if it sent one, found nothing to replace.
            await self._repo.session.execute(
                text(
                    "UPDATE file_ops SET result_node_id = :node, "
                    "result = result || CAST(:agreed AS jsonb) "
                    "WHERE id = :id AND org_team_id = :org"
                ),
                {
                    "node": landed,
                    "agreed": json.dumps({"if_match": agreed}),
                    "id": op_id,
                    "org": self._repo.scope.org_team_id,
                },
            )
        return landed

    async def _adopt(
        self, parent_id: NodeId, name: bytes, op_id: OperationId, *, holder: bool = False
    ) -> tuple[NodeId, int] | None:
        """The live same-name file the commit writes its version onto, and its etag.

        ``None`` when the folder holds no such file — the name is free, or it
        is held by a folder or a symlink, and a create decides what happens
        next rather than this quietly turning a directory into a file.

        The precondition the caller agreed the commit against is checked here
        and not in ``put_version``: ``put_version`` fences against whatever the
        node's etag is *now*, which a concurrent writer would have moved, so
        checking only there would let a ``replace`` clobber the version the
        caller never saw.
        """
        for row in await self._repo.siblings(parent_id):
            if row.name != name or row.kind != "file":
                continue
            expected = await self._if_match(op_id)
            if expected is not None and row.etag != expected:
                if holder:
                    # The holder's base is behind a write the drive took from
                    # the web. Not refused: its stale base rides to the version
                    # commit, which settles the two writes under the fence.
                    return NodeId(row.id), expected
                raise PreconditionFailed(message=f"node {row.id} moved on")
            return NodeId(row.id), row.etag
        return None

    async def _name_taken(self, row: _SessionRow) -> bool:
        """Whether a live node in the session's folder already has its name."""
        found = await self._repo.session.execute(
            text(
                "SELECT 1 FROM file_nodes n JOIN file_upload_sessions s "
                "ON s.id = :session AND s.org_team_id = :org "
                "WHERE n.parent_id = :parent AND n.name = s.name "
                "AND n.org_team_id = :org AND n.trashed_at IS NULL LIMIT 1"
            ),
            {"session": row.id, "parent": row.parent_id, "org": self._repo.scope.org_team_id},
        )
        return found.first() is not None

    async def _name(self, session_id: SessionId) -> bytes:
        statement = select(FileUploadSession.name).where(FileUploadSession.id == session_id)
        return bytes((await self._repo.execute_scoped(statement)).scalar_one())

    async def _conflict(self, op_id: OperationId) -> CommitBehavior:
        statement = text(
            "SELECT result ->> 'conflict' FROM file_ops WHERE id = :id AND org_team_id = :org"
        )
        chosen = (
            await self._repo.session.execute(
                statement, {"id": op_id, "org": self._repo.scope.org_team_id}
            )
        ).scalar()
        if chosen == "rename":
            return "rename"
        return "replace" if chosen == "replace" else "fail"

    async def _if_match(self, op_id: OperationId) -> int | None:
        """The etag the caller agreed the commit against, if it named one."""
        statement = text(
            "SELECT result ->> 'if_match' FROM file_ops WHERE id = :id AND org_team_id = :org"
        )
        chosen = (
            await self._repo.session.execute(
                statement, {"id": op_id, "org": self._repo.scope.org_team_id}
            )
        ).scalar()
        return None if chosen is None else int(chosen)

    async def _refuse(self, session_id: SessionId) -> None:
        """Give the room back and leave the staged objects for the sweeper."""
        async with self._repo.transaction():
            await self._quota.release(session_id)
            await self._repo.session.execute(
                update(_SESSIONS)
                .where(
                    FileUploadSession.id == session_id,
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                )
                .values(state="aborted")
            )

    @staticmethod
    async def _counted(stream: AsyncIterator[bytes], progress: Progress) -> AsyncIterator[bytes]:
        """``stream``, with every chunk counted onto the operation.

        Reading the staged parts back out is the whole of a promote's work and
        the only place one of its bytes moves, so this is where the operation
        proves it is alive. Counted after the chunk is yielded, because a byte
        is not moved until the writer has taken it.
        """
        async for chunk in stream:
            yield chunk
            await progress.bytes_moved(len(chunk))

    async def _concatenated(self, session_id: SessionId) -> AsyncIterator[bytes]:
        """The parts as one stream, in part order — the commit hashes it once."""
        async with self._repo.transaction():
            accepted = await self._accepted(session_id)
        for row in accepted:
            # A part that is not there is asked for at its current key anyway,
            # so the store's own refusal surfaces exactly as it always has.
            key = await self._staged_key(session_id, row) or part_key(
                session_id, row.part_no, row.checksum
            )
            stream = await self._store.get(key)
            async for chunk in stream:
                yield chunk

    async def _drop_parts(self, session_id: SessionId) -> None:
        async with self._repo.transaction():
            accepted = await self._accepted(session_id)
        await _delete_staged(self._store, session_id, accepted)

    # ---- the sweeper -----------------------------------------------------

    async def sweep_expired(self, now: datetime) -> int:
        """Expire idle sessions and unstick abandoned commits.

        The state flip and the hold release are one statement, so a session is
        never expired while still holding bytes; the staged objects go
        afterwards and one at a time, because a store that refuses one deletion
        must not keep the other sessions' rooms reserved.
        """
        swept: list[SessionId] = []
        async with self._repo.transaction():
            statement = (
                update(_SESSIONS)
                .where(
                    FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                    FileUploadSession.state.in_(PART_ACCEPTING_STATES),
                    FileUploadSession.expires_at <= now,
                )
                .values(state="expired", quota_hold_bytes=0, quota_hold_nodes=0)
                .returning(FileUploadSession.id)
            )
            result = await self._repo.session.execute(statement)
            swept = [SessionId(value) for value in result.scalars().all()]
            await self._checkpoints.reach("uploads.after_expire")
        for session_id in swept:
            await self._drop_parts(session_id)
        return len(swept) + await self._redrive_stuck(now)

    async def _redrive_stuck(self, now: datetime) -> int:
        """Give a stuck ``committing`` session an operation, or fail it.

        "Stuck" is a session whose commit has no live operation left — the
        worker that owned it is gone. Re-driving costs one queued row; a
        session whose parts are no longer in the store cannot be re-driven and
        is failed instead, releasing its hold.
        """
        redriven = 0
        async with self._repo.transaction():
            statement = text(
                "SELECT s.id, s.drive_id FROM file_upload_sessions s "
                "WHERE s.org_team_id = :org AND s.state = 'committing' "
                "AND s.created_at <= :deadline AND NOT EXISTS ("
                "  SELECT 1 FROM file_ops o WHERE o.org_team_id = s.org_team_id "
                "  AND o.result ->> 'session_id' = s.id::text "
                "  AND o.state IN ('queued', 'running'))"
            )
            rows = (
                await self._repo.session.execute(
                    statement,
                    {
                        "org": self._repo.scope.org_team_id,
                        "deadline": now - COMMITTING_DEADLINE,
                    },
                )
            ).all()
            for row in rows:
                session_id = SessionId(row[0])
                accepted = await self._accepted(session_id)
                if not accepted:
                    await self._quota.release(session_id)
                    await self._repo.session.execute(
                        update(_SESSIONS)
                        .where(
                            FileUploadSession.id == session_id,
                            FileUploadSession.org_team_id == self._repo.scope.org_team_id,
                        )
                        .values(state="aborted")
                    )
                    redriven += 1
                    continue
                state = await self._ops.start(
                    "upload", drive_id=DriveId(row[1]), total=len(accepted)
                )
                await self._bind(
                    state.id, session_id=session_id, conflict=await self._conflict(state.id)
                )
                redriven += 1
        return redriven


def _agree(parts: Sequence[PartRef], accepted: Sequence[_PartRow]) -> None:
    """Refuse a client list that is not exactly what the server accepted."""
    claimed = {part.part_no: (part.size, bytes(part.checksum)) for part in parts}
    held = {row.part_no: (row.size, bytes(row.checksum)) for row in accepted}
    if claimed != held:
        raise Conflict(
            "files.parts_mismatch",
            "the completing part list disagrees with the accepted parts",
        )


async def _accepted_part_rows(repo: FilesRepo, session_id: SessionId) -> tuple[_PartRow, ...]:
    statement = (
        select(FileUploadPart.part_no, FileUploadPart.size, FileUploadPart.checksum)
        .where(FileUploadPart.session_id == session_id)
        .order_by(FileUploadPart.part_no)
    )
    rows = (await repo.execute_scoped(statement)).all()
    return tuple(_PartRow(int(row[0]), int(row[1]), bytes(row[2])) for row in rows)


async def _delete_staged(
    store: DomainStore, session_id: SessionId, rows: Sequence[_PartRow]
) -> None:
    """Drop every staged object ``rows`` name, best effort: a store that refuses
    one deletion leaves it for the reconciliation sweeper."""
    for row in rows:
        for key in staged_part_keys(session_id, row.part_no, row.checksum):
            with contextlib.suppress(StoreError):
                await store.delete(key)


async def _maybe_session_row(repo: FilesRepo, session_id: SessionId) -> _SessionRow | None:
    """The row as Postgres holds it now, not as the identity map cached it."""
    statement = select(
        FileUploadSession.id,
        FileUploadSession.drive_id,
        FileUploadSession.parent_id,
        FileUploadSession.state,
        FileUploadSession.declared_size,
        FileUploadSession.bytes_received,
        FileUploadSession.expires_at,
        FileUploadSession.transfer_mode,
        FileUploadSession.lease_epoch,
        FileUploadSession.lease_holder,
        FileUploadSession.lease_holder_kind,
        FileUploadSession.conflict_copy,
        FileUploadSession.conflict_of,
    ).where(FileUploadSession.id == session_id)
    row = (await repo.execute_scoped(statement)).one_or_none()
    return None if row is None else _SessionRow(*row)


async def _session_row(repo: FilesRepo, session_id: SessionId) -> _SessionRow:
    row = await _maybe_session_row(repo, session_id)
    if row is None:
        raise NotFound(message=f"upload session {session_id}")
    return row


#: Where an upload's operation names the session it commits, in the operation's
#: own ``result`` document: bound when the commit is queued, read by whichever
#: runner lands it and by a reader deciding what the operation concerns.
OPERATION_SESSION_KEY = "session_id"


def session_of_operation(state: OperationState) -> SessionId | None:
    """The upload session an operation commits, or ``None`` for an operation of
    another kind or one bound to none."""
    if state.kind != "upload":
        return None
    raw = state.result.get(OPERATION_SESSION_KEY)
    if not raw:
        return None
    try:
        return SessionId(uuid.UUID(str(raw)))
    except ValueError:
        return None


async def upload_parent(repo: FilesRepo, session_id: SessionId) -> NodeId | None:
    """The folder an upload session was opened against, or ``None``.

    The node an upload operation concerns before it has produced one: a
    reader deciding whether it may follow the operation is decided on this
    folder, exactly as the commit re-authorizes against it before it lands.
    """
    row = await _maybe_session_row(repo, session_id)
    return None if row is None else NodeId(row.parent_id)


__all__ = [
    "COMMITTING_DEADLINE",
    "PART_SIZE",
    "CommitBehavior",
    "ConflictSubmission",
    "PartRef",
    "PartResult",
    "Reauthorize",
    "SessionStatus",
    "UploadCompletion",
    "UploadService",
    "UploadSession",
    "legacy_part_key",
    "part_key",
    "parts_total",
    "session_of_operation",
    "staged_part_keys",
    "upload_parent",
]
