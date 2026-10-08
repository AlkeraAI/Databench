"""One page of a folder, cut by keyset, in at most three statements.

The listing budget is 100,000 children and a page of 500 inside 150 ms, which
is only met if the page is an index-only scan on
`(drive_id, parent_id, name_key)` and no work is per row: no offset, no count,
no authorization query per item. So a page is exactly three statements — the
parent's ancestor chain, the rows, and the distinct ACLs those rows point at —
and the caller's readability is a predicate this module accepts and puts into
the rows statement, never a filter applied after the page is cut.

A listing is not a snapshot. The marker contract is the weaker, honest one Box
and Graph publish: an entry unchanged for the whole enumeration appears exactly
once; an entry created, renamed, moved or deleted meanwhile may appear zero,
once or twice. `delta` is where a consistent enumeration lives.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.sql.expression import ColumnElement

from alkera_core.authz.principal import ActingContext
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import ListFilters, Marker, OrderBy, OrderField
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.tree import FileNode

#: The published cap. A larger page is refused rather than clamped: a client
#: that asked for 5,000 and silently got 1,000 would page wrongly.
MAX_LIMIT: Final = 1000

#: A predicate the authorization layer supplies so the page is cut *after*
#: readability, and hidden siblings cannot be inferred from a short page.
ReadablePredicate = Callable[[Any], ColumnElement[bool] | None]


#: One shared no-op checkpoints object; the seam costs a coroutine step in
#: production and a test passes its own `PausingCheckpoints` instead.
_NO_CHECKPOINTS: Final = NoopCheckpoints()


def all_readable(_node_table: Any) -> ColumnElement[bool] | None:
    """The default hook: no authorization narrowing (the repo's org scope only)."""
    return None


@dataclass(frozen=True, slots=True)
class Page:
    """One page: the rows, the ACLs they point at, the chain, and the next cursor."""

    items: Sequence[FileNode]
    acls: Mapping[uuid.UUID, FileAcl]
    chain_ids: Sequence[uuid.UUID]
    next_marker: Marker | None
    #: The ids on this page the *caller* starred. It rides the rows statement as
    #: one more selected column rather than a second `SELECT … WHERE node_id IN
    #: (…)`, because the budget below is three statements and a per-caller flag
    #: is not worth a fourth.
    starred_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def has_more(self) -> bool:
        return self.next_marker is not None


def _order_value(row: FileNode, order: OrderBy) -> str | int:
    if order.field is OrderField.NAME:
        return row.name_key
    if order.field is OrderField.SIZE:
        return row.size
    if order.field is OrderField.MTIME:
        return row.mtime_ns
    return row.kind


async def children(
    repo: FilesRepo,
    ctx: ActingContext,
    parent_id: uuid.UUID,
    *,
    order_by: OrderBy | None = None,
    filters: ListFilters | None = None,
    marker: Marker | None = None,
    limit: int = 100,
    readable_predicate: ReadablePredicate = all_readable,
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> Page:
    """One keyset page of `parent_id`'s children.

    `ctx` is carried for the authorization hook and the audit trail; this
    function itself decides nothing about access — `readable_predicate` is
    where the policy lands, inside the rows statement.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise InvalidRequest("files.bad_limit", f"limit must be 1..{MAX_LIMIT}, got {limit}")
    order = order_by or OrderBy()
    wanted = filters or ListFilters()
    if marker is not None and (marker.parent_id != parent_id or marker.order != order):
        raise InvalidRequest("files.invalid_marker", "marker belongs to another query")

    # 1 of 3: the parent's derived `path_ids` carries its ancestors, so the item
    # shape's `parentReference` and the ACL walk both come free from one
    # self-join here rather than from a per-item lookup. The labels are per-drive
    # inos, so the ids come from the join, never from parsing the path.
    chain_ids = list(await repo.chain_ids(NodeId(parent_id)))
    if not chain_ids:
        raise NotFound()
    await checkpoints.reach("listing.after_chain")

    conditions: list[ColumnElement[bool]] = [FileNode.parent_id == parent_id]
    # Live rows only unless the caller asked for the trash view; the two are
    # separate indexes and a listing never mixes them.
    if wanted.trashed:
        conditions.append(FileNode.trashed_at.is_not(None))
    else:
        conditions.append(FileNode.trashed_at.is_(None))
    conditions.extend(wanted.predicates(FileNode))
    narrowed = readable_predicate(FileNode)
    if narrowed is not None:
        conditions.append(narrowed)
    if marker is not None:
        conditions.append(marker.predicate())

    # Imported here, not at module scope: `stars` reads this module's `Page`
    # and `all_readable`, so a top-level import back the other way is a cycle.
    from alkera_core.files.stars import starred_by

    column = order.column
    ordering = [column.desc() if order.descending else column.asc(), FileNode.id.asc()]
    # One row over the page so "is there another page" costs no second query,
    # and the caller's own star as one more column so it costs none either.
    rows_stmt = (
        repo.select_nodes()
        .add_columns(starred_by(FileNode, ctx.effective_user_id).label("starred_by_caller"))
        .where(*conditions)
        .order_by(*ordering)
        .limit(limit + 1)
    )
    # 2 of 3
    fetched_rows = list((await repo.execute_scoped(rows_stmt)).all())
    fetched = [row[0] for row in fetched_rows]
    await checkpoints.reach("listing.after_rows")

    more = len(fetched) > limit
    items = fetched[:limit]
    starred_ids = frozenset(row[0].id for row in fetched_rows[:limit] if row.starred_by_caller)
    next_marker = (
        Marker(
            parent_id=parent_id,
            order=order,
            value=_order_value(items[-1], order),
            last_id=items[-1].id,
        )
        if more and items
        else None
    )

    # 3 of 3: one row per distinct permission set on the page, never one query
    # per item.
    acl_ids = {row.acl_id for row in items if row.acl_id is not None}
    acls: dict[uuid.UUID, FileAcl] = {}
    if acl_ids:
        acl_stmt = repo.select_acls().where(FileAcl.id.in_(acl_ids))
        acls = {acl.id: acl for acl in (await repo.execute_scoped(acl_stmt)).scalars().all()}
    await checkpoints.reach("listing.after_acls")

    return Page(
        items=items,
        acls=acls,
        chain_ids=chain_ids,
        next_marker=next_marker,
        starred_ids=starred_ids,
    )


__all__ = ["MAX_LIMIT", "Page", "ReadablePredicate", "all_readable", "children"]
