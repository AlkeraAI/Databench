"""A text file co-edited live, as the drive sees it.

The Loro lane's ``file`` document type (:mod:`backend.services.crdt`) keeps a
file's content while people edit it together; at rest the file on the drive
is the truth. The lane asks the drive three things, answered here through the
Files policy and the content service like any other Files caller:

* who may read and who may write the file now (:func:`document_access`). A
  writer is a person the Files policy lets WRITE the node, and only while a
  write that holds no fence can land on it: no live lease covers it, or the
  lease that does takes inbound writes there (a chat's box applies them);
* the file's text at its head, refused when it is not editable text
  (:func:`read_head`);
* writing a document's text back as a new version, under a precondition on
  the version the document last matched (:func:`write_back`). Under a box's
  lease the version lands as an inbound write the box applies through its
  existing path, so an old box serves it unchanged;
* writing it back as the machine holding the file's folder, when what is
  unsaved is that machine's own agent's edit and no person can be named for
  it (:func:`write_back_as_holder`): the box's own upload, made by the server
  under the box's fence, so it lands as the holder's bytes;
* keeping a document's content beside the file as a conflicted copy, when
  the file stops being something a session can hold (:func:`keep_aside`).

Everything runs in the caller's transaction, through a joined repo: the lane
holds the document's row lock across a write back, so two replicas never
write the same document back at once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from alkera_core.authz import CredentialKind
from alkera_core.authz import authorize as decide
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import authorize
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.clock import SystemClock
from alkera_core.files.conflicts import free_conflicted_copy_name
from alkera_core.files.content import ContentService
from alkera_core.files.errors import FilesError, NotFound, PreconditionFailed
from alkera_core.files.freshness import HolderFacet, landed_state
from alkera_core.files.ids import DomainId, DriveId, NodeId, VersionId
from alkera_core.files.leases import (
    LeaseConflict,
    LeaseContext,
    admits_unfenced_write,
    covering_lease,
    fenced_write_for,
    holder_identity,
    is_hand_back,
)
from alkera_core.files.namespace import Namespace
from alkera_core.files.quota import FROZEN_CODE
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sniff import sniff_bytes
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.files.context import node_scope, write_ceilings
from backend.services.files.decided import authorized_node
from backend.services.files.facts import (
    background_facts,
    drive_confinement,
    verified_machine_id,
)
from backend.services.files.store import store_factory

#: What the decision row of a write back records instead of an HTTP verb.
WRITE_BACK_METHOD: Final = "LIVE_DOCUMENT_WRITE_BACK"
#: The label of the machine a write back as the folder's holder acts as.
HOLDER_WRITE_LABEL: Final = "live_document_holder"
#: What the decision row of a copy kept beside a co-edited file records.
KEEP_ASIDE_METHOD: Final = "LIVE_DOCUMENT_KEEP_ASIDE"


@dataclass(frozen=True, slots=True)
class DocumentAccess:
    can_read: bool
    can_write: bool


NO_ACCESS: Final = DocumentAccess(can_read=False, can_write=False)


@dataclass(frozen=True, slots=True)
class HeadText:
    """A file's text at its head version."""

    etag: int
    #: ``None`` for a file with no version yet (empty).
    version_id: uuid.UUID | None
    #: The drive's content hash of that version (``""`` without one).
    content_hash: str
    text: str
    #: The etag the version's writer said it was made on, when it said.
    based_on: int | None = None
    #: Where in a co-edited document a write back took these bytes from
    #: (``{"epoch", "vv"}``), when one wrote them.
    origin: Mapping[str, Any] | None = None
    #: The machine that sent these bytes as the folder's lease holder.
    machine_id: str | None = None
    #: The holder could not say which version these bytes were made on.
    base_unknown: bool = False
    #: A restore of an older version, by this person: made on ``based_on``
    #: on purpose, so it is applied from exactly there.
    restored_by: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Written:
    """A write back that landed: the file's etag after it, and its version."""

    etag: int
    version_id: uuid.UUID
    content_hash: str
    #: The head already held exactly these bytes; nothing new was written.
    unchanged: bool


@dataclass(frozen=True, slots=True)
class FolderHolder:
    """The machine holding the folder a file is in, while that holder takes
    the file's writes (the write back lands there as an inbound write)."""

    lease_node_id: uuid.UUID
    epoch: int
    machine_id: str
    #: The file's path under the leased folder (``/``-joined names, the lease
    #: node's own name left out), the way the holder addresses it on its disk;
    #: ``None`` when a name on the way is not text the holder can spell.
    path: str | None = None
    #: The drive the file is on.
    drive_id: uuid.UUID | None = None


class NotEditableError(Exception):
    """The file cannot be co-edited: ``reason`` is ``gone`` (not a live file
    any more), ``too_large`` or ``binary``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotLandedError(Exception):
    """The machine holding the file's folder has reported its bytes but they
    have not reached the drive yet (the row was minted ahead of them). Nothing
    a session could start from is on the drive; the same read moments later
    finds the bytes."""


class WriteBackRefusedError(Exception):
    """A write back the drive refused: ``reason`` is ``stale`` (the file
    moved past the precondition), ``leased`` (a lease covers it that takes no
    inbound write there) or ``refused`` (anything else, ``code`` says what)."""

    def __init__(self, reason: str, code: str = "") -> None:
        super().__init__(f"{reason}: {code}" if code else reason)
        self.reason = reason
        self.code = code


async def _repo(db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID) -> FilesRepo:
    """A repo for ``ctx``'s reads and writes of the file ``node_id``, scoped
    as every Files route scopes it (:func:`node_scope`): a box on its machine
    credential reads in the org of the drive it serves, never in its own
    operator org. Raises :class:`NotEditableError` (``gone``) for a node the
    caller cannot reach."""
    try:
        scope = await node_scope(db, ctx, NodeId(node_id))
    except NotFound:
        raise NotEditableError("gone") from None
    return FilesRepo.joined(db, scope)


async def document_access(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID
) -> DocumentAccess:
    """What ``ctx`` may do with the file ``node_id`` now, by the Files policy.

    Decided by the policy engine without a decision row: it is asked at every
    subscribe and every write of a live document, as the chat channel's grant
    is. A write back, which changes the drive, is decided on the record
    (:func:`write_back`). A box on its machine credential is decided on the
    confinement every route holds it to on the file's drive."""
    facts = await _socket_facts(db, ctx, node_id)
    if facts is None:
        return NO_ACCESS
    repo = await _repo(db, ctx, node_id)
    async with repo.transaction():
        try:
            written = await authorize(
                ctx, repo, NodeId(node_id), FilesAction.WRITE, facts=facts, enforce=decide
            )
        except FilesError:
            written = None
        if written is not None:
            node = written.node
            if node.kind != "file" or node.trashed_at is not None:
                return NO_ACCESS
            return DocumentAccess(can_read=True, can_write=await admits_unfenced_write(repo, node))
        try:
            read = await authorize(
                ctx, repo, NodeId(node_id), FilesAction.READ, facts=facts, enforce=decide
            )
        except FilesError:
            return NO_ACCESS
    if read.node.kind != "file" or read.node.trashed_at is not None:
        return NO_ACCESS
    return DocumentAccess(can_read=True, can_write=False)


async def _socket_facts(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID
) -> AccessFacts | None:
    """``ctx``'s facts for a decision on ``node_id`` with no request behind
    it; ``None`` when a box names a file on no drive it can reach."""
    facts = await background_facts(db, ctx)
    if not ctx.is_machine:
        return facts
    try:
        repo = await _repo(db, ctx, node_id)
    except NotEditableError:
        return None
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        drive = None if node is None else await repo.drive(DriveId(node.drive_id))
    if drive is None:
        return None
    proven = replace(facts, agent_machine_id=await verified_machine_id(db, ctx))
    return await drive_confinement(db, ctx, proven, drive)


def editable_text(data: bytes) -> str:
    """``data`` as text when it is text a person can edit; raises
    :class:`NotEditableError` (``binary``) otherwise: not UTF-8, a NUL byte,
    or what the drive's own sniffer reads as binary."""
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotEditableError("binary") from exc
    if "\x00" in text or sniff_bytes(data).mime_class == "other":
        raise NotEditableError("binary")
    return text


async def read_head(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID, *, max_bytes: int
) -> HeadText:
    """The file's text at its head version. Raises :class:`NotEditableError`
    when it is gone, larger than ``max_bytes`` or not editable text, and
    :class:`NotLandedError` while its holder's bytes are still on their way
    to the drive. The caller has already decided ``ctx`` may read it."""
    repo = await _repo(db, ctx, node_id)
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        if node is None or node.kind != "file" or node.trashed_at is not None:
            raise NotEditableError("gone")
        drive = await repo.drive(DriveId(node.drive_id))
        version = (
            await repo.version(VersionId(node.head_version_id))
            if node.head_version_id is not None
            else None
        )
        etag = int(node.etag)
        facet = (
            HolderFacet(size=int(node.holder_size), mtime_ns=int(node.holder_mtime_ns or 0))
            if node.holder_size is not None
            else None
        )
    if drive is None:
        raise NotEditableError("gone")
    if version is None and facet is not None and facet.size > 0:
        if landed_state(None, facet) == "unlanded":
            # Reading it now would start the document from an empty text the
            # holder never wrote: a new notebook would read as no notebook.
            raise NotLandedError()
    if version is None:
        return HeadText(etag=etag, version_id=None, content_hash="", text="")
    if int(version.size_bytes) > max_bytes:
        raise NotEditableError("too_large")
    store = await store_factory(settings).for_domain(DomainId(drive.dedup_domain_id))
    service = ContentService(repo, ctx, SystemClock(), store)
    stream = await service.open(VersionId(version.id))
    data = b"".join([chunk async for chunk in stream])
    metadata = version.version_metadata or {}
    based_on = metadata.get("based_on_etag")
    origin = metadata.get("origin")
    machine = metadata.get("machine_id")
    return HeadText(
        etag=etag,
        version_id=uuid.UUID(str(version.id)),
        content_hash=str(version.content_hash),
        text=editable_text(data),
        based_on=based_on if isinstance(based_on, int) else None,
        origin=origin if isinstance(origin, dict) else None,
        machine_id=machine if isinstance(machine, str) and machine else None,
        base_unknown=metadata.get("base_unknown") is True,
        restored_by=(
            uuid.UUID(str(version.created_by))
            if version.source == "restore" and version.created_by is not None
            else None
        ),
    )


async def written_positions(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID, *, limit: int
) -> list[tuple[int, Mapping[str, Any]]]:
    """The file's latest versions a co-edited document wrote back, newest
    first, as ``(etag, origin)``: the etag the version was published at (its
    write named the one before as its precondition, and a publish moves the
    etag by one) and where in the document its text was taken from."""
    repo = await _repo(db, ctx, node_id)
    async with repo.transaction():
        # Only the two keys: a version's metadata also lists every block of
        # its content, which a long window of large files would read in full.
        rows = await repo.execute_scoped(
            select(
                FileVersion.version_metadata["based_on_etag"].label("based_on"),
                FileVersion.version_metadata["origin"].label("origin"),
            )
            .where(
                FileVersion.node_id == node_id,
                FileVersion.source == "document_snapshot",
            )
            .order_by(FileVersion.seq.desc())
            .limit(limit)
        )
        found: list[tuple[int, Mapping[str, Any]]] = []
        for based_on, origin in rows:
            if isinstance(based_on, int) and isinstance(origin, dict):
                found.append((based_on + 1, origin))
    return found


async def head_etag(db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID) -> int | None:
    """The file's etag now; ``None`` when it is not a live file."""
    try:
        repo = await _repo(db, ctx, node_id)
    except NotEditableError:
        return None
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
    if node is None or node.kind != "file" or node.trashed_at is not None:
        return None
    return int(node.etag)


async def folder_holder(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID
) -> FolderHolder | None:
    """Who holds the folder the file ``node_id`` is in, when a live lease
    covers it and takes the file's writes; ``None`` otherwise. Read without a
    lock: it says whom to tell that the file's live document moved, and
    fences nothing."""
    try:
        repo = await _repo(db, ctx, node_id)
    except NotEditableError:
        return None
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        if node is None or node.kind != "file" or node.trashed_at is not None:
            return None
        lease = await covering_lease(repo, node, lock="none")
        if lease is None or not await admits_unfenced_write(repo, node):
            return None
        path = await _path_under(repo, node, uuid.UUID(str(lease.node_id)))
    return FolderHolder(
        lease_node_id=uuid.UUID(str(lease.node_id)),
        epoch=int(lease.epoch),
        machine_id=str(lease.machine_id),
        path=path,
        drive_id=uuid.UUID(str(node.drive_id)),
    )


#: The deepest a file may sit under its leased folder for its path to be spelled.
_PATH_DEPTH: Final = 256


async def _path_under(repo: FilesRepo, node: Any, lease_node_id: uuid.UUID) -> str | None:
    """``node``'s names from just below ``lease_node_id`` down to it, joined
    by ``/``; ``None`` when the walk does not reach the lease node or a name
    is not UTF-8 (the holder could not spell it)."""
    names: list[str] = []
    current = node
    for _ in range(_PATH_DEPTH):
        if uuid.UUID(str(current.id)) == lease_node_id:
            return "/".join(reversed(names)) or None
        raw = current.name
        try:
            name = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except UnicodeDecodeError:
            return None
        names.append(name)
        if current.parent_id is None:
            return None
        current = await repo.node(NodeId(uuid.UUID(str(current.parent_id))))
        if current is None:
            return None
    return None


async def holder_peer(
    db: AsyncSession, ctx: ActingContext, node_id: uuid.UUID, *, lease: LeaseContext | None
) -> str:
    """The agent a text peer of the file ``node_id`` writes as: the machine of
    the lease that covers the file, when ``ctx`` holds that lease at the epoch
    ``lease`` names (the fence every holder write passes) and the lease takes
    the file's writes. Raises :class:`NotEditableError` (``gone``) for a file
    that is not there and :class:`LeaseConflict` for a caller that does not
    hold it. The caller decided the Files policy first."""
    repo = await _repo(db, ctx, node_id)
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        if node is None or node.kind != "file" or node.trashed_at is not None:
            raise NotEditableError("gone")
        covering = await fenced_write_for(repo, node, lease)
        if (
            covering is None
            or not is_hand_back(covering, lease)
            or not await admits_unfenced_write(repo, node)
        ):
            raise LeaseConflict("files.lease_fenced", "only the folder's holder is a text peer")
    return str(covering.machine_id)


async def _not_writable(repo: FilesRepo, node_id: uuid.UUID) -> WriteBackRefusedError:
    """Why the policy refused a write back, as far as anyone can act on it: a
    drive frozen over its storage ceiling refuses every writer alike (and is
    said as that); anything else is this writer's own."""
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        drive = None if node is None else await repo.drive(DriveId(node.drive_id))
    if drive is not None and drive.frozen_reason is not None:
        return WriteBackRefusedError("refused", FROZEN_CODE)
    return WriteBackRefusedError("refused", "files.not_writable")


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _as_written(info: Any, etag: int) -> Written:
    return Written(
        etag=etag,
        version_id=uuid.UUID(str(info.id)),
        content_hash=str(info.content_hash),
        unchanged=bool(info.unchanged),
    )


async def write_back(
    db: AsyncSession,
    ctx: ActingContext,
    node_id: uuid.UUID,
    text: str,
    *,
    if_match: int,
    origin: Mapping[str, Any] | None = None,
) -> Written:
    """Write ``text`` as the file's new head version, attributed to ``ctx``,
    provided the file is still at etag ``if_match``.

    Decided on the record as ``ctx`` (WRITE on the node), held to the drive's
    storage ceilings, and recorded with source ``document_snapshot``. Raises
    :class:`WriteBackRefusedError`; never writes over a version it did not
    know about."""
    repo = await _repo(db, ctx, node_id)
    allowed = await authorized_node(
        db,
        repo,
        ctx,
        NodeId(node_id),
        FilesAction.WRITE,
        method=WRITE_BACK_METHOD,
        path=f"/files/{node_id}",
    )
    if allowed is None:
        raise await _not_writable(repo, node_id)
    async with repo.transaction():
        drive = await repo.drive(DriveId(allowed.node.drive_id))
    if drive is None:
        raise WriteBackRefusedError("refused", "files.not_found")
    data = text.encode("utf-8")
    store = await store_factory(settings).for_domain(DomainId(drive.dedup_domain_id))
    service = ContentService(repo, ctx, SystemClock(), store, ceilings=write_ceilings(ctx, drive))
    try:
        info = await service.put_version(
            NodeId(node_id),
            _one(data),
            size_declared=len(data),
            if_match=if_match,
            source="document_snapshot",
            origin=origin,
        )
    except PreconditionFailed as exc:
        raise WriteBackRefusedError("stale", exc.code) from exc
    except LeaseConflict as exc:
        raise WriteBackRefusedError("leased", exc.code) from exc
    except FilesError as exc:
        raise WriteBackRefusedError("refused", exc.code) from exc
    etag = await head_etag(db, ctx, node_id)
    if etag is None:
        raise WriteBackRefusedError("refused", "files.not_found")
    return _as_written(info, etag)


@dataclass(frozen=True, slots=True)
class _Holder:
    """The machine holding a file's folder, as the server acts as it: its
    context, the facts its own request would carry, and its lease's fence."""

    ctx: ActingContext
    facts: AccessFacts
    fence: LeaseContext


async def _holder_of(
    db: AsyncSession, org_id: uuid.UUID, node: Any, drive: Any, *, machine_id: str | None
) -> _Holder:
    """The machine holding the folder ``node`` is in, read from its lease row
    (never from anything a request sent). ``machine_id`` names the machine it
    must be. Raises :class:`WriteBackRefusedError` (``leased``) when no
    machine holds it, or another one does."""
    repo = await _repo(db, _lane_reader(org_id, uuid.UUID(str(node.id))), uuid.UUID(str(node.id)))
    async with repo.transaction():
        lease = await covering_lease(repo, node, lock="none")
    if lease is None or lease.holder_kind != "machine":
        raise WriteBackRefusedError("leased", "files.no_machine_holder")
    holder = str(lease.holder_principal_id)
    if (machine_id is not None and machine_id != holder) or str(lease.machine_id) != holder:
        raise WriteBackRefusedError("leased", "files.no_machine_holder")
    ctx = ActingContext.for_machine(
        machine_id=uuid.UUID(holder),
        credential_id=uuid.UUID(holder),
        org_id=org_id,
        label=HOLDER_WRITE_LABEL,
    )
    return _Holder(
        ctx=ctx,
        facts=AccessFacts(
            is_agent=True,
            agent_machine_id=holder,
            leased_subtree=uuid.UUID(str(drive.root_node_id)),
            held_leases=frozenset({uuid.UUID(str(lease.node_id))}),
        ),
        fence=LeaseContext(
            epoch=int(lease.epoch),
            instance_id=str(lease.holder_instance_id),
            base_known=True,
            holder=holder_identity(ctx, verified_machine_id=holder),
        ),
    )


def _lane_reader(org_id: uuid.UUID, node_id: uuid.UUID) -> ActingContext:
    """The lane reading the drive's rows to decide whom to write as: it
    decides nothing and writes nothing as itself."""
    return ActingContext.for_service(
        token_id=node_id,
        org_id=org_id,
        label=HOLDER_WRITE_LABEL,
        credential=CredentialKind.CI_TOKEN,
    )


async def _live_file(db: AsyncSession, org_id: uuid.UUID, node_id: uuid.UUID) -> tuple[Any, Any]:
    """The file ``node_id`` and its drive; refused when it is not a live file."""
    repo = await _repo(db, _lane_reader(org_id, node_id), node_id)
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        if node is None or node.kind != "file" or node.trashed_at is not None:
            raise WriteBackRefusedError("refused", "files.not_found")
        drive = await repo.drive(DriveId(node.drive_id))
    if drive is None:
        raise WriteBackRefusedError("refused", "files.not_found")
    return node, drive


async def write_back_as_holder(
    db: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    text: str,
    *,
    if_match: int,
    machine_id: str | None,
    origin: Mapping[str, Any] | None = None,
) -> Written:
    """Write ``text`` as the file's new head version as the machine holding
    the folder it is in, under that holder's fence, provided the file is
    still at etag ``if_match``.

    For a co-edited document whose unsaved content is the holder's own
    agent's edit (merged through the text peer, so the box never uploaded
    it), when no person can be named for it: the bytes land exactly as the
    box's own upload of them would, stamped with its machine, its live row
    settled. ``machine_id`` is the machine that made the edit; the write is
    refused (``leased``) when another holds the folder now, or none does,
    or the holder is not a machine. ``None`` takes whichever machine holds
    it (the edit's machine is no longer on record).

    Decided on the record as that machine, with the facts its own request
    would carry: the lease it holds over the file. Raises
    :class:`WriteBackRefusedError`."""
    node, drive = await _live_file(db, org_id, node_id)
    if int(node.etag) != if_match:
        # The holder's own write is the one write the drive does not hold to
        # its precondition (it settles a stale base by keeping the head it
        # displaces as a copy). A write back must merge first instead.
        raise WriteBackRefusedError("stale", "files.precondition_failed")
    holder = await _holder_of(db, org_id, node, drive, machine_id=machine_id)
    repo = await _repo(db, holder.ctx, node_id)
    allowed = await authorized_node(
        db,
        repo,
        holder.ctx,
        NodeId(node_id),
        FilesAction.WRITE,
        method=WRITE_BACK_METHOD,
        path=f"/files/{node_id}",
        facts=holder.facts,
    )
    if allowed is None:
        raise await _not_writable(repo, node_id)
    data = text.encode("utf-8")
    store = await store_factory(settings).for_domain(DomainId(drive.dedup_domain_id))
    service = ContentService(
        repo, holder.ctx, SystemClock(), store, ceilings=write_ceilings(holder.ctx, drive)
    )
    try:
        info = await service.put_version(
            NodeId(node_id),
            _one(data),
            size_declared=len(data),
            if_match=if_match,
            lease=holder.fence,
            source="document_snapshot",
            origin=origin,
        )
    except PreconditionFailed as exc:
        raise WriteBackRefusedError("stale", exc.code) from exc
    except LeaseConflict as exc:
        raise WriteBackRefusedError("leased", exc.code) from exc
    except FilesError as exc:
        raise WriteBackRefusedError("refused", exc.code) from exc
    etag = await head_etag(db, _lane_reader(org_id, node_id), node_id)
    if etag is None:
        raise WriteBackRefusedError("refused", "files.not_found")
    return _as_written(info, etag)


#: Who a copy kept beside a co-edited file is named as having displaced.
KEPT_ASIDE_BY: Final = "live edit"


async def keep_aside(
    db: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    text: str,
    *,
    person: ActingContext | None,
    machine_id: str | None = None,
) -> uuid.UUID:
    """Write ``text`` as a new file beside ``node_id``, named as a conflicted
    copy of it: a co-edited document's content that its file can no longer
    hold (the file grew past what a session opens, or became binary).

    Written as ``person``, or, when ``person`` is ``None``, as the machine
    holding the folder (``machine_id`` when it must be that one). Either way
    it holds no fence: under a box's lease that takes outside writes it lands
    as an inbound write the box applies to its disk, like a file dropped in
    the browser. Decided on the record (WRITE on the folder). The new file's
    id; raises :class:`WriteBackRefusedError`."""
    repo = await _repo(db, _lane_reader(org_id, node_id), node_id)
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        if node is None or node.parent_id is None:
            raise WriteBackRefusedError("refused", "files.not_found")
        drive = await repo.drive(DriveId(node.drive_id))
    if drive is None:
        raise WriteBackRefusedError("refused", "files.not_found")
    if person is not None:
        ctx, facts = person, None
    else:
        holder = await _holder_of(db, org_id, node, drive, machine_id=machine_id)
        ctx, facts = holder.ctx, holder.facts
    repo = await _repo(db, ctx, node_id)
    parent = NodeId(uuid.UUID(str(node.parent_id)))
    allowed = await authorized_node(
        db,
        repo,
        ctx,
        parent,
        FilesAction.WRITE,
        method=KEEP_ASIDE_METHOD,
        path=f"/files/{node_id}",
        facts=facts,
    )
    if allowed is None:
        raise await _not_writable(repo, node_id)
    clock = SystemClock()
    ceilings = write_ceilings(ctx, drive)
    namespace = Namespace(repo, ctx, clock, ceilings=ceilings)

    async def make() -> FileNode:
        taken = {bytes(row.name) for row in await repo.siblings(parent)}
        name = free_conflicted_copy_name(
            bytes(node.name), KEPT_ASIDE_BY, clock.now(), taken.__contains__
        )
        return await namespace.create(
            DriveId(node.drive_id), parent, "file", name, conflict="rename"
        )

    try:
        store = await store_factory(settings).for_domain(DomainId(drive.dedup_domain_id))
        service = ContentService(repo, ctx, clock, store, ceilings=ceilings)
        copy, _ = await service.put_first_version(
            DriveId(node.drive_id), make, text.encode("utf-8")
        )
    except LeaseConflict as exc:
        raise WriteBackRefusedError("leased", exc.code) from exc
    except FilesError as exc:
        raise WriteBackRefusedError("refused", exc.code) from exc
    return uuid.UUID(str(copy.id))


__all__ = [
    "NO_ACCESS",
    "WRITE_BACK_METHOD",
    "DocumentAccess",
    "FolderHolder",
    "HeadText",
    "NotEditableError",
    "NotLandedError",
    "WriteBackRefusedError",
    "Written",
    "document_access",
    "editable_text",
    "folder_holder",
    "head_etag",
    "holder_peer",
    "keep_aside",
    "read_head",
    "write_back",
    "write_back_as_holder",
    "written_positions",
]
