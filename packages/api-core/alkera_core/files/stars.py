"""Starring: one bit in `file_nodes.flags`, set and cleared by compare-and-swap.

A star is a per-node boolean too small to earn a column, so it lives in the
`flags` bitfield `filters.FLAG_STARRED` names. Both writes are a single
``UPDATE … WHERE`` whose predicate carries the state it expects, so two clients
racing to star the same node cannot lose one another's bit and cannot both
report having changed it — the one that updated zero rows already had the state
it wanted, which is success with nothing to announce.

That "nothing to announce" is why a no-op write emits no history and no outbox
row: an announcement of a change that did not happen is exactly what the outbox
contract forbids.

The bit answers "does anyone star this node", which is what the `starred=` chip
narrows a listing by. *Who* starred it is the `file_stars` row this module
writes alongside the bit, and it is what the wire's per-item `starred` reads: a
star is one person's bookmark, so the same node reads starred for its owner and
unstarred for a colleague. :func:`starred_by` is that read as a correlated
`EXISTS`, so a listing carries it in the statement that already fetched the rows
rather than in a second query per page.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import exists, select

from alkera_core.authz.principal import ActingContext
from alkera_core.files import history
from alkera_core.files.errors import NotFound
from alkera_core.files.filters import (
    FLAG_STARRED,
    ListFilters,
    Marker,
    OrderBy,
    starred_by,
)
from alkera_core.files.history import actor_ref
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.listing import Page, ReadablePredicate, all_readable
from alkera_core.files.repo import FilesRepo
from alkera_core.files.search import (
    anchor_for,
    base_conditions,
    check_limit,
    check_marker,
    keyset_page,
)
from alkera_core.models.files.history import FileStar
from alkera_core.models.files.tree import FileNode


async def _set_star(
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    *,
    wanted: bool,
) -> bool:
    """Move the caller's own star, and the node bit that summarises everyone's."""
    # A star is now two facts — the caller's row and the node bit — so the node
    # is resolved once up front rather than only on the miss path: writing one
    # caller's bookmark against a node in another org would be a leak, and a
    # trashed node is a no-op rather than a 404 whichever fact is asked about.
    node = await repo.node(node_id)
    if node is None:
        raise NotFound()
    if node.trashed_at is not None:
        return False

    # The caller's own row is the per-user truth and moves first, so the node
    # bit below can be cleared on the strength of what is left rather than on
    # the strength of the one row this call just removed.
    own_moved = await _set_own_star(repo, ctx, node_id, wanted=wanted)

    starred = FileNode.flags.op("&")(FLAG_STARRED) != 0
    if wanted:
        bit_check = ~starred
    else:
        # The bit means "somebody stars this", so it clears only once nobody
        # does — the caller's row is already gone by here, so this sees theirs.
        bit_check = starred & ~exists(select(FileStar.node_id).where(FileStar.node_id == node_id))
    stmt = (
        repo.update_nodes()
        .where(
            FileNode.id == node_id,
            FileNode.trashed_at.is_(None),
            # The compare-and-swap: the update *is* the check, so no window
            # exists between reading the bit and writing it.
            bit_check,
        )
        .values(
            flags=(
                FileNode.flags.op("|")(FLAG_STARRED)
                if wanted
                else FileNode.flags.op("&")(~FLAG_STARRED)
            )
        )
        .returning(FileNode.id, FileNode.drive_id, FileNode.etag)
    )
    moved = (await repo.session.execute(stmt)).first()
    if moved is None and not own_moved:
        # The node as a whole already said what the caller asked for, and their
        # own bookmark did too: nothing changed, so nothing is announced.
        return False
    # When only the caller's row moved, the node row read above still carries
    # the drive and etag the announcement needs.
    row = moved if moved is not None else node
    await history.record(
        repo,
        ctx,
        node_id=node_id,
        kind="attrs",
        before={"starred": not wanted},
        after={"starred": wanted},
    )
    await history.emit_node_changed(
        repo,
        ctx,
        node_id=node_id,
        drive_id=DriveId(row.drive_id),
        version=row.etag,
    )
    return True


async def _set_own_star(
    repo: FilesRepo,
    ctx: ActingContext,
    node_id: NodeId,
    *,
    wanted: bool,
) -> bool:
    """Add or remove *this* caller's row; ``True`` when it actually moved.

    A principal with no user behind it (an agent, a service) has no bookmarks
    list to keep, so it moves the node bit and writes no row.
    """
    user_id = ctx.effective_user_id
    if user_id is None:
        return False
    return await repo.set_star_row(node_id=node_id, user_id=user_id, wanted=wanted)


async def star(repo: FilesRepo, ctx: ActingContext, node_id: NodeId) -> bool:
    """Star `node_id`; `False` when it was already starred (no history written)."""
    return await _set_star(repo, ctx, node_id, wanted=True)


async def unstar(repo: FilesRepo, ctx: ActingContext, node_id: NodeId) -> bool:
    """Unstar `node_id`; `False` when it was not starred (no history written)."""
    return await _set_star(repo, ctx, node_id, wanted=False)


async def starred(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    filters: ListFilters | None = None,
    order_by: OrderBy | None = None,
    marker: Marker | None = None,
    limit: int = 100,
    readable_predicate: ReadablePredicate = all_readable,
) -> Page:
    """One keyset page of the caller's starred nodes.

    The star chip is the same `ListFilters` predicate every other surface
    offers, so this surface is that filter forced on — and "starred" here means
    *this caller's* rows, not the node bit: the bit says somebody starred the
    node, so a colleague's bookmark would otherwise show up under this caller's
    "Starred".
    """
    check_limit(limit)
    who = actor_ref(ctx)
    order = order_by or OrderBy()
    anchor = anchor_for("starred", who)
    check_marker(marker, anchor, order)

    conditions = base_conditions(filters, readable_predicate)
    conditions.append(starred_by(FileNode, ctx.effective_user_id))
    if marker is not None:
        conditions.append(marker.predicate())
    return await keyset_page(
        repo,
        conditions,
        anchor=anchor,
        order=order,
        limit=limit,
        starred_for=ctx.effective_user_id,
    )


def star_bit_of(flags: int) -> bool:
    """Whether a node's `flags` value carries the star. The one reader outside SQL."""
    return bool(flags & FLAG_STARRED)


__all__: Sequence[str] = ["star", "star_bit_of", "starred", "starred_by", "unstar"]
