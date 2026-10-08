"""Closing a conflict: the head swap, the history row and the announcement.

Separate from :mod:`alkera_core.files.conflicts` on purpose. That module is the
pure *naming* algorithm — bytes in, bytes out, no clock and no I/O — shared by
the browser, the mount and the materializer, and its purity is what lets the
property tests hammer it. This module is the opposite kind of thing: it reads
and writes rows in the caller's transaction, takes a clock, and settles quota.
Folding the two together would drag a session and a clock into the algorithm
every façade imports, so they stay apart.

The reason this exists at all: a resolution promotes a version to the head, and
a head swap that leaves no ``file_history`` row and no outbox row is invisible
to the delta feed. A client that resolved a conflict on one device would never
learn about it on another, and the browser's refresh path would show the losing
bytes until something else touched the node. So the promotion, the history row
and the announcement are written here, in one transaction, exactly as every
other Files mutation does it.

History kind: the row is written as ``conflict_resolved`` — its own kind, not
the ``attrs`` a new version takes in :mod:`alkera_core.files.content` — with the
resolution named in the ``after`` snapshot, so the history pane and the delta
reader can tell a promotion apart from an ordinary attribute change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from sqlalchemy import CursorResult, select

from alkera_core.authz.principal import ActingContext
from alkera_core.files.clock import Clock
from alkera_core.files.conflicts import conflicted_copy_name
from alkera_core.files.errors import Conflict, NotFound, PreconditionFailed
from alkera_core.files.history import actor_ref, emit_node_changed, record
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import LeaseContext, admitted_inbound, fenced_write_for
from alkera_core.files.namespace import Namespace
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import CONFLICT_ACTIONABLE_STATES, FileConflict
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion

__all__ = [
    "CONFLICT_PAGE",
    "ConflictChoice",
    "ConflictRecord",
    "Resolution",
    "get_conflict",
    "list_open_conflicts",
    "resolve_conflict",
]

#: How many open conflicts one page carries. The pane is a work queue, not a
#: feed: a drive with more is paged by the client asking again after resolving.
CONFLICT_PAGE: Final = 200

#: Which side wins. ``both`` promotes theirs and keeps mine as a real sibling
#: under the conflicted-copy name, so no bytes are ever dropped silently.
ConflictChoice = Literal["mine", "theirs", "both"]


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """One divergence, by id. File names never appear here, so a record leaks no name.

    ``who`` is who wrote the bytes that kept the name (the last arrival, from
    the side ``arrived_from`` names) and ``displaced_by`` whose bytes went to
    the copy — names as a person reads them, a member's or a machine's, which
    the copy's own file name already says to anyone who can list its folder.
    """

    id: uuid.UUID
    node_id: uuid.UUID
    base_version_id: uuid.UUID | None
    theirs_version_id: uuid.UUID
    mine_version_id: uuid.UUID
    state: str
    #: The conflicted copy holding the displaced bytes, when the drive made one.
    copy_node_id: uuid.UUID | None = None
    #: Which side's write reached the drive last and kept the name.
    arrived_from: str | None = None
    #: Who wrote the bytes that kept the name.
    who: str | None = None
    #: Whose bytes were displaced (and named the copy).
    displaced_by: str | None = None
    #: Who closed it, once someone has.
    resolved_by: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a resolve left behind: the new head, and the sibling if one was made."""

    id: uuid.UUID
    node_id: uuid.UUID
    state: str
    head_version_id: uuid.UUID
    etag: int
    copy_node_id: uuid.UUID | None


def _record(row: FileConflict) -> ConflictRecord:
    return ConflictRecord(
        id=row.id,
        node_id=row.node_id,
        base_version_id=row.base_version_id,
        theirs_version_id=row.theirs_version_id,
        mine_version_id=row.mine_version_id,
        state=row.state,
        copy_node_id=row.copy_node_id,
        arrived_from=row.arrived_from,
        who=row.who,
        displaced_by=row.displaced_by,
        resolved_by=row.resolved_by,
    )


def _parent_of(node: FileNode) -> NodeId | None:
    """The folder a node stands in, for a lock chain that takes it first."""
    return None if node.parent_id is None else NodeId(node.parent_id)


async def list_open_conflicts(
    repo: FilesRepo, drive_id: DriveId, *, limit: int = CONFLICT_PAGE
) -> list[ConflictRecord]:
    """This drive's open conflicts, oldest id first, in the caller's transaction.

    Unfiltered by readability on purpose: the caller applies the effective role
    per item before it cuts the page, which is what keeps a hidden sibling from
    changing the size of anyone's page.
    """
    rows = (
        (
            await repo.execute_scoped(
                repo.select_conflicts()
                .where(
                    FileConflict.state.in_(CONFLICT_ACTIONABLE_STATES),
                    FileConflict.node_id.in_(
                        select(FileNode.id).where(FileNode.drive_id == drive_id)
                    ),
                )
                .order_by(FileConflict.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_record(row) for row in rows]


async def get_conflict(repo: FilesRepo, conflict_id: uuid.UUID) -> ConflictRecord | None:
    """One conflict by id, or ``None`` — the caller turns that into its 404."""
    row = (
        await repo.execute_scoped(repo.select_conflicts().where(FileConflict.id == conflict_id))
    ).scalar_one_or_none()
    return None if row is None else _record(row)


async def resolve_conflict(
    repo: FilesRepo,
    ctx: ActingContext,
    conflict_id: uuid.UUID,
    *,
    choice: ConflictChoice,
    if_match: int | None,
    clock: Clock,
    lease: LeaseContext | None = None,
) -> Resolution:
    """Close one conflict by promoting a side, in the caller's transaction.

    The promotion is a compare-and-swap on the node's etag, so a write that
    landed between the caller reading the etag and this call losing the race is
    a ``412`` rather than a silent overwrite of someone else's head.

    Fenced, because promoting a side IS a head swap and ``both`` creates a
    sibling besides. This pane is where a fenced holder's divergent work lands,
    so it is the one route a second writer must not be able to reach into a
    live mount through: the holder resolves under its own epoch, and everybody
    else waits for the folder to be handed back.
    """
    found = await get_conflict(repo, conflict_id)
    if found is None:
        raise NotFound()
    if found.state not in CONFLICT_ACTIONABLE_STATES:
        raise Conflict("files.conflict_resolved", "That conflict is already resolved")
    if found.state == "auto":
        return await _confirm_auto(
            repo, ctx, found, choice=choice, if_match=if_match, clock=clock, lease=lease
        )

    # The whole chain up front, drive first, because `choice == "both"` goes on
    # to create a sibling and `Namespace.create` takes the drive itself: opening
    # with a bare node lock would have this transaction ask for drive-after-node
    # while every other Files mutation asks for drive-before-node, which is the
    # cycle the detector cuts with 40P01. The drive is read off the node rather
    # than the conflict row, which does not carry it. The parent comes before
    # the node for the same reason: a rename holds its folder and then writes
    # the node, and this transaction writes into that folder once it holds
    # the node -- the sibling, and the folder's child counts.
    unlocked = await repo.node(NodeId(found.node_id))
    if unlocked is None:  # pragma: no cover - the row's FK guarantees the node exists
        raise NotFound()
    _, _, node = await repo.lock_chain(
        DriveId(unlocked.drive_id), _parent_of(unlocked), NodeId(found.node_id)
    )
    if node is None:  # pragma: no cover - the row was just read in this transaction
        raise NotFound()
    await fenced_write_for(repo, node, lease)
    before_etag = int(node.etag)
    if if_match is not None and if_match != before_etag:
        raise PreconditionFailed(message=f"node {found.node_id} has changed")

    head_id = found.mine_version_id if choice == "mine" else found.theirs_version_id
    versions = {version.id: version for version in await repo.versions_of(NodeId(found.node_id))}
    head = versions.get(head_id)
    if head is None:  # pragma: no cover - the row's FK guarantees the version exists
        raise NotFound()

    copy_node_id = (
        await _keep_mine_as_sibling(
            repo,
            ctx,
            clock,
            node=node,
            versions=versions,
            mine_id=found.mine_version_id,
            lease=lease,
        )
        if choice == "both"
        else None
    )

    # Read before the swap: `_promote` refreshes the ORM row, so afterwards
    # these fields already hold the values the `after` snapshot describes.
    before_size = int(node.size)
    before_head = node.head_version_id
    after_etag = await _promote(repo, clock, node=node, head=head, if_match=before_etag)
    await repo.session.execute(
        repo.update_conflicts()
        .where(FileConflict.id == conflict_id)
        .values(state="resolved", resolved_at=clock.now(), resolved_by=actor_ref(ctx))
    )
    await record(
        repo,
        ctx,
        node_id=NodeId(found.node_id),
        kind="conflict_resolved",
        before={
            "etag": before_etag,
            "size": before_size,
            "head_version_id": None if before_head is None else str(before_head),
        },
        after={
            "etag": after_etag,
            "size": int(head.size_bytes),
            "head_version_id": str(head_id),
            "conflict_id": str(conflict_id),
            "conflict_resolution": choice,
        },
    )
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(found.node_id),
        drive_id=DriveId(node.drive_id),
        version=after_etag,
    )
    # A node carrying a head version always has a parent: only the drive root
    # is parentless, and a root is a folder with no versions to resolve.
    if node.parent_id is not None:
        await QuotaService(repo, ctx, clock).settle_head_swap(
            DriveId(node.drive_id),
            NodeId(node.parent_id),
            bytes_delta=int(head.size_bytes) - before_size,
        )
    return Resolution(
        id=conflict_id,
        node_id=found.node_id,
        state="resolved",
        head_version_id=head_id,
        etag=after_etag,
        copy_node_id=copy_node_id,
    )


async def _confirm_auto(
    repo: FilesRepo,
    ctx: ActingContext,
    found: ConflictRecord,
    *,
    choice: ConflictChoice,
    if_match: int | None,
    clock: Clock,
    lease: LeaseContext | None,
) -> Resolution:
    """Close a conflict the drive already settled, the way a person chose.

    The drive left the last arrival under the name and the displaced bytes in
    a conflicted copy (or only in the node's history). What a person decides
    now is which of the two stays:

    * ``mine`` — keep this: the name keeps what it holds, the copy is trashed;
    * ``theirs`` — keep the other: the displaced bytes become the head again
      and the copy is trashed;
    * ``both`` — keep both: nothing moves, the row closes.

    Every step is undoable: a trashed copy comes back from the trash, and a
    head the displaced bytes replace stays a version of the node.
    """
    unlocked = await repo.node(NodeId(found.node_id))
    if unlocked is None:  # pragma: no cover - an auto conflict's node is its FK target
        raise NotFound()
    # Keeping one side trashes the copy, which rewrites a subtree: the tree
    # is taken exclusive now, since asking for it after a shared hold waits
    # on every other writer that holds it shared.
    _, _, node = await repo.lock_chain(
        DriveId(unlocked.drive_id),
        _parent_of(unlocked),
        NodeId(found.node_id),
        rewrites_subtree=choice != "both",
    )
    if node is None:  # pragma: no cover - the auto conflict's node was just read here
        raise NotFound()
    covering = await fenced_write_for(repo, node, lease)
    before_etag = int(node.etag)
    if if_match is not None and if_match != before_etag:
        raise PreconditionFailed(message=f"node {found.node_id} has changed")
    before_size = int(node.size)
    before_head = node.head_version_id
    after_etag = before_etag
    head_id = node.head_version_id
    if choice == "theirs" and node.head_version_id != found.theirs_version_id:
        versions = {version.id: version for version in await repo.versions_of(NodeId(node.id))}
        head = versions.get(found.theirs_version_id)
        if head is None:  # pragma: no cover - the displaced version is the row's FK target
            raise NotFound()
        after_etag = await _promote(repo, clock, node=node, head=head, if_match=before_etag)
        head_id = head.id
        if node.parent_id is not None:
            await QuotaService(repo, ctx, clock).settle_head_swap(
                DriveId(node.drive_id),
                NodeId(node.parent_id),
                bytes_delta=int(head.size_bytes) - before_size,
            )
        if admitted_inbound(covering) is not None:
            # The machine holding the folder still has the other bytes on its
            # disk: it takes these like any write the drive accepted for it.
            await LiveEntriesService(repo, ctx).accept_inbound(node, kind="inbound")
    trashed_copy = None
    if choice in ("mine", "theirs") and found.copy_node_id is not None:
        trashed_copy = await _trash_copy(repo, ctx, clock, found.copy_node_id, lease=lease)
    await repo.session.execute(
        repo.update_conflicts()
        .where(FileConflict.id == found.id)
        .values(state="resolved", resolved_at=clock.now(), resolved_by=actor_ref(ctx))
    )
    await record(
        repo,
        ctx,
        node_id=NodeId(found.node_id),
        kind="conflict_resolved",
        before={
            "etag": before_etag,
            "size": before_size,
            "head_version_id": None if before_head is None else str(before_head),
        },
        after={
            "etag": after_etag,
            "size": int(node.size),
            "head_version_id": None if head_id is None else str(head_id),
            "conflict_id": str(found.id),
            "conflict_resolution": choice,
            "copy_trashed": None if trashed_copy is None else str(trashed_copy),
        },
    )
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(found.node_id),
        drive_id=DriveId(node.drive_id),
        version=after_etag,
        parent_id=None if node.parent_id is None else NodeId(node.parent_id),
        reason="conflict",
    )
    return Resolution(
        id=found.id,
        node_id=found.node_id,
        state="resolved",
        head_version_id=head_id if head_id is not None else found.mine_version_id,
        etag=after_etag,
        copy_node_id=found.copy_node_id,
    )


async def _trash_copy(
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    copy_node_id: uuid.UUID,
    *,
    lease: LeaseContext | None,
) -> uuid.UUID | None:
    """Trash the conflicted copy, restorably; ``None`` when it is already gone.

    A copy someone already trashed, purged or moved to the trash with its
    folder has nothing left to trash, and the decision still closes the row.
    """
    # Deferred: the trash module imports the ops module, which reaches back
    # into the write services that import this one.
    from alkera_core.files.trash import Trash

    copy = await repo.node(NodeId(copy_node_id))
    if copy is None or copy.trashed_at is not None:
        return None
    await Trash(repo, ctx, clock).trash(NodeId(copy.id), if_match=int(copy.etag), lease=lease)
    return copy.id


async def _keep_mine_as_sibling(
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    *,
    node: FileNode,
    versions: dict[uuid.UUID, FileVersion],
    mine_id: uuid.UUID,
    lease: LeaseContext | None = None,
) -> uuid.UUID | None:
    """Create the conflicted copy holding the caller's side, under its own name.

    A node with no parent is a drive root, which cannot have a sibling; there is
    nowhere to put the copy, so ``both`` degrades to ``theirs`` rather than
    inventing a home for it.

    The caller's lease rides along because the sibling is a create into the
    folder the conflicted node sits in, and the namespace fences that on its
    own. Without it ``both`` — the one resolution that throws nothing away —
    was the one resolution nobody could run inside a mount, holder included.
    """
    if node.parent_id is None:
        return None
    mine = versions.get(mine_id)
    copy = await Namespace(repo, ctx, clock).create(
        DriveId(node.drive_id),
        NodeId(node.parent_id),
        "file",
        conflicted_copy_name(node.name, str(ctx.acting_principal.id), clock.now()),
        conflict="rename",
        lease=lease,
    )
    await repo.session.execute(
        repo.update_nodes()
        .where(FileNode.id == copy.id)
        .values(
            head_version_id=mine_id,
            size=int(mine.size_bytes) if mine is not None else 0,
        )
    )
    return copy.id


async def _promote(
    repo: FilesRepo, clock: Clock, *, node: FileNode, head: FileVersion, if_match: int
) -> int:
    """Point the node at ``head`` iff its etag is still ``if_match``."""
    result = cast(
        CursorResult[Any],
        await repo.session.execute(
            repo.update_nodes()
            .where(FileNode.id == node.id, FileNode.etag == if_match)
            .values(
                head_version_id=head.id,
                size=int(head.size_bytes),
                etag=FileNode.etag + 1,
                mtime_ns=int(clock.now().timestamp() * 1_000_000_000),
            )
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - the row is locked FOR UPDATE above
        raise PreconditionFailed(message=f"node {node.id} moved on")
    await repo.session.refresh(node)
    return if_match + 1
