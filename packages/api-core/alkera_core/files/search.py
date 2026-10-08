"""Name search, recent and shared-with-me: three keyset surfaces over one index.

All three are *listings*: every predicate a caller
can observe, including their readability, is a SQL predicate applied **before**
the page is cut. A row the caller may not read is never fetched and then
dropped, because a page that came back one short would tell them a hidden
sibling exists.

`search_names` is a substring match on `name_key`, which is
``NFC(full_casefold(display(name)))`` — so ``readme`` finds ``README`` and an
NFD-composed query finds its NFC twin without the caller folding anything.
The trigram GIN index on `name_key` serves the `LIKE '%…%'`; the cap below is a
`LIMIT` on the inner scan, never a cap on results: a query matching a million
nodes still returns a full page, cut out of the first `SEARCH_SCAN_CAP` rows
the index yields rather than reading the whole drive.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import and_, func, not_, or_, select, text
from sqlalchemy.sql.expression import ColumnElement

from alkera_core.authz.principal import ActingContext
from alkera_core.files import names
from alkera_core.files.drives import SHARED_NAME
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import (
    Direction,
    ListFilters,
    Marker,
    OrderBy,
    OrderField,
    starred_by,
)
from alkera_core.files.history import actor_ref
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.listing import MAX_LIMIT, Page, ReadablePredicate, all_readable
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode

#: How many index rows one search may touch. A cap on *scanned* rows, not on
#: results: the page is still full when the drive holds more matches, and the
#: keyset marker resumes into the next window.
SEARCH_SCAN_CAP: Final = 20_000

#: How far back `recent` looks. The window closes inside the statement with
#: Postgres `now()`, so every row of a page is judged against one instant.
RECENT_DAYS: Final = 30

#: Marker anchors. A `Marker` carries the query it was cut from so a cursor
#: cannot be replayed against a different one; these surfaces have no parent
#: folder to anchor to, so each derives a stable per-caller anchor instead —
#: a `recent` cursor replayed against `sharedWithMe` is refused.
ANCHOR_NS: Final = uuid.UUID("6bd5e3ee-6c2a-4f31-9d51-2b0a6b4e9a10")


def anchor_for(surface: str, who: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(ANCHOR_NS, f"{surface}:{who}")


def touched_within_window(who: uuid.UUID) -> list[ColumnElement[bool]]:
    """One principal's own history rows inside the window.

    The half of `recent`'s filter that reads `file_history`, spelled once:
    `ix_file_history_org_principal_at` exists to serve exactly these columns in
    exactly this order, and a second spelling of the predicate is how a
    statement and the index meant for it drift apart. The window closes with
    Postgres `now()` inside the statement, so it is the same instant for every
    row of the page.
    """
    return [
        FileHistory.acting_principal == who,
        FileHistory.at >= func.now() - text(f"interval '{RECENT_DAYS} days'"),
    ]


def check_limit(limit: int) -> None:
    if limit < 1 or limit > MAX_LIMIT:
        raise InvalidRequest("files.bad_limit", f"limit must be 1..{MAX_LIMIT}, got {limit}")


def check_marker(marker: Marker | None, anchor: uuid.UUID, order: OrderBy) -> None:
    if marker is not None and (marker.parent_id != anchor or marker.order != order):
        raise InvalidRequest("files.invalid_marker", "marker belongs to another query")


def _order_value(row: FileNode, order: OrderBy) -> str | int:
    if order.field is OrderField.NAME:
        return row.name_key
    if order.field is OrderField.SIZE:
        return row.size
    if order.field is OrderField.MTIME:
        return row.mtime_ns
    return row.kind


def _escape_like(fragment: str) -> str:
    """`%`, `_` and the escape itself are literals in a user's search string."""
    return fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def base_conditions(
    filters: ListFilters | None,
    readable_predicate: ReadablePredicate,
) -> list[ColumnElement[bool]]:
    wanted = filters or ListFilters()
    conditions: list[ColumnElement[bool]] = []
    if wanted.trashed:
        conditions.append(FileNode.trashed_at.is_not(None))
    else:
        conditions.append(FileNode.trashed_at.is_(None))
    conditions.extend(wanted.predicates(FileNode))
    # The readability predicate joins here, inside the rows statement and before
    # the LIMIT, so an unreadable row never changes a page's size.
    narrowed = readable_predicate(FileNode)
    if narrowed is not None:
        conditions.append(narrowed)
    return conditions


async def keyset_page(
    repo: FilesRepo,
    conditions: Sequence[ColumnElement[bool]],
    *,
    anchor: uuid.UUID,
    order: OrderBy,
    limit: int,
    scan_cap: int | None = None,
    chain_ids: Sequence[uuid.UUID] = (),
    starred_for: uuid.UUID | None = None,
) -> Page:
    """The rows statement and the ACL statement — the shape every surface shares.

    `starred_for` rides the rows statement as one more selected column rather
    than a second `SELECT … WHERE node_id IN (…)` per page: every surface built
    on this function renders the caller's own star, and none of them spends a
    statement on it.
    """
    ordering = [
        order.column.desc() if order.descending else order.column.asc(),
        FileNode.id.asc(),
    ]
    rows_stmt = repo.select_nodes().where(*conditions).order_by(*ordering)
    if scan_cap is not None:
        # The cap is an inner LIMIT so the planner stops walking the index; the
        # outer LIMIT still cuts a full page out of what the scan found.
        scanned = rows_stmt.limit(scan_cap).subquery()
        matched = select(scanned.c.id)
        rows_stmt = repo.select_nodes().where(FileNode.id.in_(matched)).order_by(*ordering)
    # One row over the page, so "is there another page" costs no second query,
    # and the caller's own star as one more column so it costs none either.
    rows_stmt = rows_stmt.add_columns(starred_by(FileNode, starred_for).label("starred_by_caller"))
    fetched_rows = list((await repo.execute_scoped(rows_stmt.limit(limit + 1))).all())
    fetched = [row[0] for row in fetched_rows]

    more = len(fetched) > limit
    items = fetched[:limit]
    starred_ids = frozenset(row[0].id for row in fetched_rows[:limit] if row.starred_by_caller)
    next_marker = (
        Marker(
            parent_id=anchor,
            order=order,
            value=_order_value(items[-1], order),
            last_id=items[-1].id,
        )
        if more and items
        else None
    )
    acl_ids = {row.acl_id for row in items if row.acl_id is not None}
    acls: dict[uuid.UUID, FileAcl] = {}
    if acl_ids:
        acl_stmt = repo.select_acls().where(FileAcl.id.in_(acl_ids))
        acls = {acl.id: acl for acl in (await repo.execute_scoped(acl_stmt)).scalars().all()}
    return Page(
        items=items,
        acls=acls,
        chain_ids=list(chain_ids),
        next_marker=next_marker,
        starred_ids=starred_ids,
    )


async def search_names(
    repo: FilesRepo,
    ctx: ActingContext,
    drive_id: DriveId,
    q: str,
    *,
    scope_node_id: NodeId | None = None,
    filters: ListFilters | None = None,
    order_by: OrderBy | None = None,
    marker: Marker | None = None,
    limit: int = 100,
    readable_predicate: ReadablePredicate = all_readable,
) -> Page:
    """One keyset page of the nodes in `drive_id` whose name contains `q`.

    Case- and accent-insensitive because both sides fold the same way: the
    stored `name_key` when the node was written, the query by `names.name_key`
    here. Nothing in this function compares raw bytes.
    """
    check_limit(limit)
    if not q.strip():
        raise InvalidRequest("files.bad_query", "search needs a non-empty query")
    order = order_by or OrderBy()
    anchor = uuid.UUID(str(scope_node_id or drive_id))
    check_marker(marker, anchor, order)

    conditions = base_conditions(filters, readable_predicate)
    conditions.append(FileNode.drive_id == drive_id)
    folded = names.name_key(q.encode())
    conditions.append(FileNode.name_key.like(f"%{_escape_like(folded)}%", escape="\\"))
    # A search never returns the drive root or another traversal-only container:
    # they hold no name a caller could have typed.
    conditions.append(FileNode.traversal_only.is_(False))

    chain_ids: Sequence[uuid.UUID] = ()
    if scope_node_id is not None:
        # Statement 1 of 3: the scope folder's chain — and the reason a scope in
        # another org and a nonexistent one give the same `not found`.
        chain_ids = await repo.chain_ids(scope_node_id)
        scope = await repo.node(scope_node_id) if chain_ids else None
        if scope is None:
            raise NotFound()
        # The repo owns the one spelling of "at or under this node", so the
        # scope predicate and the index that serves it cannot drift apart.
        conditions.append(repo.subtree_predicate(scope))
        conditions.append(FileNode.id != scope.id)
    if marker is not None:
        conditions.append(marker.predicate())

    return await keyset_page(
        repo,
        conditions,
        anchor=anchor,
        order=order,
        limit=limit,
        scan_cap=SEARCH_SCAN_CAP,
        chain_ids=chain_ids,
        starred_for=ctx.effective_user_id,
    )


async def recent(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    filters: ListFilters | None = None,
    marker: Marker | None = None,
    limit: int = 100,
    readable_predicate: ReadablePredicate = all_readable,
) -> Page:
    """The nodes this caller changed in the last 30 days, newest first.

    A projection over `file_history`, not a table of its own: "recent" is
    whatever the caller's own history rows still say inside the window, so
    nothing has to be written, trimmed or reconciled when a node moves or is
    trashed.
    """
    check_limit(limit)
    who = actor_ref(ctx)
    order = OrderBy(field=OrderField.MTIME, direction=Direction.DESC)
    anchor = anchor_for("recent", who)
    check_marker(marker, anchor, order)

    conditions = base_conditions(filters, readable_predicate)
    touched = (
        select(FileHistory.node_id)
        .where(FileHistory.node_id == FileNode.id, *touched_within_window(who))
        .exists()
    )
    conditions.append(touched)
    # The drive's skeleton is structure, not something the person changed, even
    # though provisioning stamped the first caller's name on its history: the
    # root and the traversal-only containers (``home/``, ``Teams/``), and the
    # org's ``Shared/`` beside them. Without this a fresh drive's Recent opened
    # with an unnamed row (the root), home, Teams and Shared above the files.
    conditions.append(FileNode.traversal_only.is_(False))
    conditions.append(not_(and_(FileNode.depth == 1, FileNode.name == SHARED_NAME)))
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


async def shared_with_me(
    repo: FilesRepo,
    ctx: ActingContext,
    *,
    principal_ids: Sequence[uuid.UUID] = (),
    filters: ListFilters | None = None,
    order_by: OrderBy | None = None,
    marker: Marker | None = None,
    limit: int = 100,
) -> Page:
    """The *roots* of the live grants made directly to this caller.

    `principal_ids` are the caller's other principals — their teams and their
    org — resolved by the authorization layer, the only thing that knows the
    membership chain. A grant on a folder inside another granted folder is not
    a second entry: the caller reaches it by opening the first.

    No readability predicate: here a direct grant *is* the readability, so
    every row returned is a node the caller was handed on purpose.
    """
    check_limit(limit)
    who = actor_ref(ctx)
    order = order_by or OrderBy()
    anchor = anchor_for("shared_with_me", who)
    check_marker(marker, anchor, order)

    mine = {who, *principal_ids}
    # Statement 1: the granted node ids. Live grants only — a revoked or an
    # expired share is not shared with anybody.
    shares_stmt = repo.select_shares().where(
        FileShare.principal_id.in_(mine),
        FileShare.revoked_at.is_(None),
        or_(FileShare.expires_at.is_(None), FileShare.expires_at > func.now()),
    )
    granted = {share.node_id for share in (await repo.execute_scoped(shares_stmt)).scalars().all()}
    if not granted:
        return Page(items=[], acls={}, chain_ids=[], next_marker=None)

    # Statement 2: their paths, so a grant under another grant drops out.
    paths_stmt = repo.select_nodes().where(FileNode.id.in_(granted), FileNode.trashed_at.is_(None))
    candidates = list((await repo.execute_scoped(paths_stmt)).scalars().all())
    prefixes = {node.path_ids for node in candidates}
    roots = [
        node.id
        for node in candidates
        if not any(
            node.path_ids != other and node.path_ids.startswith(f"{other}.") for other in prefixes
        )
    ]
    if not roots:
        return Page(items=[], acls={}, chain_ids=[], next_marker=None)

    conditions = base_conditions(filters, all_readable)
    conditions.append(FileNode.id.in_(roots))
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


__all__ = [
    "RECENT_DAYS",
    "SEARCH_SCAN_CAP",
    "recent",
    "search_names",
    "shared_with_me",
    "touched_within_window",
]
