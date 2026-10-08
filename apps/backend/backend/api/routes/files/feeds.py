"""The feeds: search, recent, starred, sharedWithMe and one node's activity.

Every route here is a *listing*, so all five obey the same rule: a node the
caller may not read is absent from the page **and** the page is still full.
Filtering after the cut would turn a short page into a count of what is
hidden, which would be an oracle on it.

The library takes readability as a SQL predicate applied inside the rows
statement, before the ``LIMIT``. A decision, though, is asynchronous — it loads
the chain and calls ``enforce`` — so it cannot be evaluated inside that
statement. The two are reconciled the way the delta route reconciles them: read
a window of candidates, decide the whole window in one batch, then read the
page again with the decided ids as the predicate. The second read is the one
that pages, so the ``LIMIT`` still lands after readability and the marker is
still a keyset cut of the readable set.

The batch carries each node's chain and effective access, so the render uses
the answer the decision was made from rather than a chain re-read that could
disagree with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Annotated, Any

from alkera_core.db.session import get_db
from alkera_core.files import lease_snapshots
from alkera_core.files import search as search_lib
from alkera_core.files import stars as stars_lib
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.authz.readable import access_by_id, decided_by_id
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.filters import Direction, ListFilters, Marker, OrderBy, OrderField
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.listing import MAX_LIMIT, Page, ReadablePredicate, all_readable
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.files.item import Item
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import as_platform, ratelimited
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.deps.files_lane import authorized
from backend.api.deps.files_nodes import _order, _uuid
from backend.api.params import PathId
from backend.services.files.context import FilesContext
from backend.services.files.items import (
    to_item,
    with_lease,
    with_location,
    with_object_facets,
    with_owner_names,
    with_path,
)

router = APIRouter(tags=["files"])

#: How many candidates one page may consider before deciding them. A page of
#: ``limit`` readable rows needs more than ``limit`` candidates whenever some
#: are hidden; this is the ceiling on how many hidden ones a single page will
#: look past, and it is capped by the listing's own maximum so a caller cannot
#: turn a feed into an unbounded scan.
CANDIDATE_FACTOR = 4

#: Query keys the feed routes consume themselves; every other key is handed to
#: ``ListFilters.from_query``, which refuses what it does not know — so a typo
#: in a filter name is an error rather than a silently wider page.
_FEED_KEYS = frozenset({"orderBy", "marker", "limit", "q", "scope", "ancestor"})

#: The activity feed's ordering: newest first, and the anchor a marker must
#: name so a cursor cut from one node cannot be replayed against another.
_ACTIVITY_ORDER = OrderBy(field=OrderField.MTIME, direction=Direction.DESC)


class ItemsPage(BaseModel):
    """One page of items, the shape every feed answers with."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: list[Item]
    next_marker: str | None = None


class ActivityEntry(BaseModel):
    """One history row as a client sees it: ids and a kind, never a name.

    "Errors carry ids, never names" is a rule about every Files body, not only
    error bodies, so the before/after payloads the library records are
    not rendered here.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    node_id: str
    seq: int
    kind: str
    at: str
    acting_principal: str
    delegating_user: str | None = None
    agent_session_id: str | None = None
    op_id: str | None = None


class ActivityPage(BaseModel):
    """One page of a node's activity."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: list[ActivityEntry]
    next_marker: str | None = None


def _drive(files: FilesContext, drive_id: uuid.UUID) -> DriveId:
    """The drive the caller named, or the opaque 404.

    A drive id that is not this org's own is indistinguishable, from outside,
    from one that does not exist.
    """
    if files.drive.id != drive_id:
        raise NotFound()
    return DriveId(files.drive.id)


def _filters(request: Request, files: FilesContext) -> ListFilters:
    """Every ``ListFilters`` key the feeds accept, read off the query."""
    params = {k: v for k, v in request.query_params.items() if k not in _FEED_KEYS}
    me = files.ctx.effective_user_id or uuid.UUID(int=0)
    return ListFilters.from_query(params, me=me)


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return 100
    try:
        return int(raw)
    except ValueError:
        raise InvalidRequest("files.bad_limit", "limit must be an integer") from None


def _marker(request: Request, *, anchor: uuid.UUID, order: OrderBy) -> Marker | None:
    raw = request.query_params.get("marker")
    return Marker.decode(raw, parent_id=anchor, order=order) if raw else None


def _candidate_limit(limit: int) -> int:
    return min(MAX_LIMIT, max(limit, limit * CANDIDATE_FACTOR))


#: A feed call: everything but how much may be read and who may read it.
FeedCall = Callable[[ReadablePredicate, int], Awaitable[Page]]


async def _decided_page(
    db: AsyncSession,
    files: FilesContext,
    call: FeedCall,
    *,
    limit: int,
    facts: AccessFacts,
) -> ItemsPage:
    """One page of a feed, cut after every row in it was authorized.

    Two reads of the same query: the first names candidates, all of which are
    decided; the second is the page, restricted to the ids that were allowed.
    Because that restriction is a predicate rather than a post-filter, the page
    that comes back is a full one whenever enough readable rows exist, and its
    marker resumes inside the readable set.

    The candidate window is decided in one batch. Deciding it a row at a time
    through ``enforce()`` cost three reads and a committed ``authz.decision``
    row per candidate — four hundred of each for a page of a hundred — so a
    feed a portal tab re-polls was seconds of round trips and an audit lane any
    member could grow without bound by leaving the tab open. ``decided_by_id``
    answers exactly what the per-node path answers for READ, over rows it has
    already read, and records nothing: a feed is a rendering pass, not an
    access attempt on every row it looked past.
    """
    candidates = await call(all_readable, _candidate_limit(limit))
    considered = [NodeId(row.id) for row in candidates.items]
    decided = await decided_by_id(files.repo, files.ctx, considered, facts=facts)
    rendered: dict[uuid.UUID, Item] = {}
    # The ids each row's lease could be held on. The lookup itself cannot run
    # inside this loop — it would be one statement per row — so the ids are
    # collected here and resolved in one statement below.
    lease_ids: list[tuple[uuid.UUID, list[uuid.UUID]]] = []
    # The ancestors' ids AND names: a feed row's path names every folder above
    # it, and the one batched decision that says which of those names this
    # caller may read can only run once every row of the page is in.
    chains: dict[uuid.UUID, list[tuple[uuid.UUID, bytes]]] = {}
    for node_id in considered:
        row = decided.get(node_id)
        # Absent means the repo's scope did not yield it — another org's, or
        # gone between the two reads — which is the same silence as unreadable.
        if row is None or not row.access.allows(FilesAction.READ):
            continue
        # The caller's own star came back on the candidates' rows statement, so
        # rendering it costs no query — and it is per user, so it cannot be read
        # off the node.
        rendered[node_id] = to_item(
            row.node,
            row.chain,
            row.access,
            starred=node_id in candidates.starred_ids,
        )
        lease_ids.append((node_id, [ancestor.id for ancestor in row.chain]))
        chains[node_id] = [(ancestor.id, bytes(ancestor.name)) for ancestor in row.chain]
    # One statement for the whole page. Without it a feed told a second member
    # that a file inside a folder someone has mounted was under no lease at all.
    snapshots = await lease_snapshots.lease_facets(
        files.repo, lease_ids, ctx=files.ctx, now=files.clock.now()
    )
    rendered = {
        node_id: with_lease(item, snapshots.get(node_id)) for node_id, item in rendered.items()
    }
    # One statement for the whole page: every row's whole chain decided at once,
    # so a caller holding a single grant deep in somebody else's home is handed
    # the file and not the names of the folders it sits in.
    chain_access = await access_by_id(
        files.repo,
        files.ctx,
        [NodeId(ancestor_id) for chain in chains.values() for ancestor_id, _name in chain],
        facts=facts,
    )
    readable_chain = {
        ancestor_id
        for ancestor_id, access in chain_access.items()
        if access.allows(FilesAction.READ)
    }
    rendered = {
        node_id: with_path(item, chains[node_id], readable=readable_chain)
        for node_id, item in rendered.items()
    }
    # A feed's rows come from every corner of the drive, so each one says which
    # folder it is in: without it two files of the same name in two folders are
    # two rows a reader cannot tell apart. It rides on the decision above, so an
    # ancestor this caller may not read names nothing.
    rendered = {
        node_id: with_location(
            item,
            chains[node_id],
            readable=readable_chain,
            root_id=files.drive.root_node_id,
        )
        for node_id, item in rendered.items()
    }
    # One statement for the whole feed, for the reason the lease lookup above
    # is one: a name per row would be a statement per row.
    # The names live in ``users``, a platform table the tenant role cannot read
    # (the session is still stamped ``alkera_files_app`` after the Files work),
    # so the one lookup steps out of the role for exactly its own statement.
    async with as_platform(db):
        named = await with_object_facets(
            db,
            await with_owner_names(
                db, list(rendered.values()), org_team_id=files.repo.scope.org_team_id
            ),
        )
    # Keyed on each item's own id rather than on its position: the fold above
    # hides a chat warmed ahead of its first message, so it hands back fewer
    # rows than it was given, and pairing by position both raised on the length
    # and — where the lengths matched — served every row after a hidden one
    # under its neighbour's id.
    rendered = {uuid.UUID(item.id): item for item in named}
    allowed = set(rendered)

    def readable(_table: Any) -> Any:
        return FileNode.id.in_(allowed)

    page = await call(readable, limit)
    ordered = [row.id for row in page.items]
    marker = page.next_marker.encode() if page.next_marker is not None else None
    return ItemsPage(
        value=[rendered[node_id] for node_id in ordered if node_id in rendered],
        next_marker=marker,
    )


@router.get(
    "/drives/{drive_id}/search",
    response_model=ItemsPage,
    dependencies=[Depends(ratelimited("search"))],
)
async def search(
    request: Request,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    drive_id: PathId,
    q: str = "",
) -> ItemsPage:
    """Name search across one drive, or a subtree of it.

    ``scope=folder:{id}`` narrows to a subtree; the scope folder is resolved by
    the library, which raises the same opaque 404 for a folder in another org
    as for one that never existed.
    """
    drive = _drive(files, _uuid(drive_id, "drive"))
    scope_raw = request.query_params.get("scope")
    scope: NodeId | None = None
    if scope_raw and scope_raw != "drive":
        head, _, tail = scope_raw.partition(":")
        if head != "folder" or not tail:
            raise InvalidRequest("files.bad_scope", "scope is drive or folder:{id}")
        scope = NodeId(_uuid(tail, "scope"))
    order = _order(request.query_params.get("orderBy"))
    anchor = uuid.UUID(str(scope or drive))
    marker = _marker(request, anchor=anchor, order=order)
    filters = _filters(request, files)

    async def call(readable: ReadablePredicate, limit: int) -> Page:
        return await search_lib.search_names(
            files.repo,
            files.ctx,
            drive,
            q,
            scope_node_id=scope,
            filters=filters,
            order_by=order,
            marker=marker,
            limit=limit,
            readable_predicate=readable,
        )

    facts = await facts_for(request, db, files.ctx, files.drive)
    async with files.repo.transaction():
        return await _decided_page(db, files, call, limit=_limit(request), facts=facts)


@router.get(
    "/drives/{drive_id}/recent",
    response_model=ItemsPage,
    dependencies=[Depends(ratelimited("recent"))],
)
async def recent(
    request: Request,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    drive_id: PathId,
) -> ItemsPage:
    """What this caller touched in the last thirty days, newest first."""
    _drive(files, _uuid(drive_id, "drive"))
    who = uuid.UUID(files.ctx.acting_principal.id)
    order = OrderBy(field=OrderField.MTIME, direction=Direction.DESC)
    marker = _marker(request, anchor=search_lib.anchor_for("recent", who), order=order)
    filters = _filters(request, files)

    async def call(readable: ReadablePredicate, limit: int) -> Page:
        return await search_lib.recent(
            files.repo,
            files.ctx,
            filters=filters,
            marker=marker,
            limit=limit,
            readable_predicate=readable,
        )

    facts = await facts_for(request, db, files.ctx, files.drive)
    async with files.repo.transaction():
        return await _decided_page(db, files, call, limit=_limit(request), facts=facts)


@router.get(
    "/drives/{drive_id}/starred",
    response_model=ItemsPage,
    dependencies=[Depends(ratelimited("starred"))],
)
async def starred(
    request: Request,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    drive_id: PathId,
) -> ItemsPage:
    """The caller's starred nodes."""
    _drive(files, _uuid(drive_id, "drive"))
    who = uuid.UUID(files.ctx.acting_principal.id)
    order = _order(request.query_params.get("orderBy"))
    marker = _marker(request, anchor=search_lib.anchor_for("starred", who), order=order)
    filters = _filters(request, files)

    async def call(readable: ReadablePredicate, limit: int) -> Page:
        return await stars_lib.starred(
            files.repo,
            files.ctx,
            filters=filters,
            order_by=order,
            marker=marker,
            limit=limit,
            readable_predicate=readable,
        )

    facts = await facts_for(request, db, files.ctx, files.drive)
    async with files.repo.transaction():
        return await _decided_page(db, files, call, limit=_limit(request), facts=facts)


@router.get(
    "/drives/{drive_id}/sharedWithMe",
    response_model=ItemsPage,
    dependencies=[Depends(ratelimited("shared_with_me"))],
)
async def shared_with_me(
    request: Request,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    drive_id: PathId,
) -> ItemsPage:
    """The roots of the grants made to this caller, their teams and their org.

    The library takes no readability predicate here because a direct grant *is*
    the readability; the principals it filters on come from the platform role
    resolver, the only thing that knows the membership chain.
    """
    _drive(files, _uuid(drive_id, "drive"))
    who = uuid.UUID(files.ctx.acting_principal.id)
    order = _order(request.query_params.get("orderBy"))
    marker = _marker(request, anchor=search_lib.anchor_for("shared_with_me", who), order=order)
    filters = _filters(request, files)
    facts = await facts_for(request, db, files.ctx, files.drive)
    principals: Sequence[uuid.UUID] = sorted(facts.team_ids)

    async def call(_readable: ReadablePredicate, limit: int) -> Page:
        return await search_lib.shared_with_me(
            files.repo,
            files.ctx,
            principal_ids=principals,
            filters=filters,
            order_by=order,
            marker=marker,
            limit=limit,
        )

    async with files.repo.transaction():
        return await _decided_page(db, files, call, limit=_limit(request), facts=facts)


@router.get(
    "/drives/{drive_id}/items/{item_id}/activity",
    response_model=ActivityPage,
    dependencies=[Depends(ratelimited("activity"))],
)
async def activity(
    request: Request,
    files: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    drive_id: PathId,
    item_id: PathId,
) -> ActivityPage:
    """One node's history page — or its subtree's, with ``ancestor=true``.

    The node is authorized first and the rows are read second, so the three
    "not yours" classes all leave through ``authorized`` with the same opaque
    404 and none of them reaches a history query.
    """
    drive = _uuid(drive_id, "drive")
    limit = _limit(request)
    if limit < 1 or limit > MAX_LIMIT:
        raise InvalidRequest("files.bad_limit", f"limit must be 1..{MAX_LIMIT}, got {limit}")
    facts = await facts_for(request, db, files.ctx, files.drive)
    async with files.repo.transaction():
        decided = await authorized(
            request,
            db,
            files.repo,
            files.ctx,
            NodeId(_uuid(item_id, "item")),
            FilesAction.READ,
            facts=facts,
        )
        if decided.node.drive_id != drive:
            raise NotFound()
        marker = _marker(request, anchor=decided.node.id, order=_ACTIVITY_ORDER)
        subtree = request.query_params.get("ancestor") == "true"
        rows = await _activity_rows(
            files, decided, subtree=subtree, marker=marker, limit=limit, facts=facts
        )

    more = len(rows) > limit
    page = rows[:limit]
    return ActivityPage(
        value=[_entry(row) for row in page],
        next_marker=(
            Marker(
                parent_id=decided.node.id,
                order=_ACTIVITY_ORDER,
                value=page[-1].at.isoformat(),
                last_id=page[-1].id,
            ).encode()
            if more and page
            else None
        ),
    )


#: How many candidate windows one subtree page may read past unreadable rows
#: before it settles for a short page. Bounds the work one request can cause.
_ACTIVITY_MAX_WINDOWS = 8


async def _activity_rows(
    files: FilesContext,
    decided: Authorized[Any],
    *,
    subtree: bool,
    marker: Marker | None,
    limit: int,
    facts: AccessFacts,
) -> list[FileHistory]:
    """The history rows for one node, or for every node under it the caller
    may read.

    The anchor's own READ says nothing about what lies under it: every member
    reads the drive root and ``home/``, so a subtree page that skipped the
    per-row decision handed out the history of everyone's private folders.
    Each candidate window is decided in one batch and the unreadable rows are
    dropped; the cursor walks on past them until the page is full.

    The statement itself lives on the repo, which is the only module that may
    name a Files table; the route decodes the marker and hands the repo the
    cursor it decoded.
    """
    cut = _parsed(str(marker.value)) if marker is not None else None
    cut_id = marker.last_id if marker is not None else None
    if not subtree:
        return list(
            await files.repo.history_page(
                decided.node, subtree=False, before=cut, before_id=cut_id, limit=limit + 1
            )
        )
    window = _candidate_limit(limit + 1)
    kept: list[FileHistory] = []
    for _ in range(_ACTIVITY_MAX_WINDOWS):
        batch = list(
            await files.repo.history_page(
                decided.node, subtree=True, before=cut, before_id=cut_id, limit=window
            )
        )
        if not batch:
            break
        access = await access_by_id(
            files.repo, files.ctx, {NodeId(row.node_id) for row in batch}, facts=facts
        )
        for row in batch:
            granted = access.get(row.node_id)
            if granted is not None and granted.allows(FilesAction.READ):
                kept.append(row)
                if len(kept) > limit:
                    return kept
        if len(batch) < window:
            break
        cut, cut_id = batch[-1].at, batch[-1].id
    return kept


def _parsed(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise InvalidRequest("files.invalid_marker", "marker is not valid") from None


def _entry(row: FileHistory) -> ActivityEntry:
    return ActivityEntry(
        id=str(row.id),
        node_id=str(row.node_id),
        seq=row.seq,
        kind=row.kind,
        at=row.at.isoformat(),
        acting_principal=str(row.acting_principal),
        delegating_user=str(row.delegating_user) if row.delegating_user else None,
        agent_session_id=str(row.agent_session_id) if row.agent_session_id else None,
        op_id=str(row.op_id) if row.op_id else None,
    )


__all__ = [
    "CANDIDATE_FACTOR",
    "ActivityEntry",
    "ActivityPage",
    "ItemsPage",
    "activity",
    "recent",
    "router",
    "search",
    "shared_with_me",
    "starred",
]
