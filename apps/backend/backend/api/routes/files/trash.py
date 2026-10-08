"""The trash view and the two things a caller does to it.

Listing is authorized per entry rather than per page: the library returns every
trashed root in the drive, and an entry whose node the caller may not read is
dropped — not rendered as a stub, because "there is something here you cannot
see" is itself the fact the opaque-404 contract withholds.

Restore and empty both decide before they act, so a refusal leaves the trash
exactly as it was; ``empty`` is one transaction in the library for the same
reason.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from alkera_core.db.session import get_db
from alkera_core.files import lease_snapshots
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, authorize
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import DriveId, NodeId, OperationId, TrashOpId
from alkera_core.files.ops import OperationState
from alkera_core.files.trash import Trash, TrashEntry
from alkera_core.schemas.files.item import Item
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    as_platform,
    files_enforcer,
    idempotent_route,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.deps.files_lane import authorized, decided_page
from backend.api.routes.files.content import fence
from backend.api.routes.files.items import with_lease_chats
from backend.api.routes.files.operations import queued_answer
from backend.services.files.context import FilesContext
from backend.services.files.guards import refuse_a_move_that_raises_the_mover
from backend.services.files.items import (
    PathPart,
    display_path,
    home_labels,
    path_parts,
    to_item,
    with_lease,
    with_lease_machines,
    with_owner_names,
)
from backend.services.files.operations_runner import queue_inline

router = APIRouter(tags=["files"])

#: How many trashed roots one page holds. The library pages by trash-op id, so a
#: marker is an op id and a page is stable under concurrent deletions.
TRASH_PAGE_LIMIT = 50


class TrashEntryWire(BaseModel):
    """One trashed root, with how long is left before the purge takes it."""

    model_config = ConfigDict(populate_by_name=True)

    trash_op_id: str = Field(serialization_alias="trashOpId")
    original_parent_id: str | None = Field(default=None, serialization_alias="originalParentId")
    #: The folder it was deleted from, as a path a person reads; ``None`` once
    #: that folder is itself gone. The id above is what a restore lands on;
    #: this is what the trash view shows under "Original location".
    original_path: str | None = Field(default=None, serialization_alias="originalPath")
    deleted_at: str = Field(serialization_alias="deletedAt")
    purge_after: str = Field(serialization_alias="purgeAfter")
    time_left_seconds: int = Field(serialization_alias="timeLeftSeconds")
    #: Why the drive trashed it when nobody asked: ``left_on_machine`` for a
    #: file whose bytes never left the machine that held its folder, with that
    #: machine in ``reasonMachine``. ``None`` for a deletion somebody made.
    reason: str | None = None
    reason_machine: str | None = Field(default=None, serialization_alias="reasonMachine")
    item: Item


class TrashPage(BaseModel):
    """One marker page of the trash view."""

    model_config = ConfigDict(populate_by_name=True)

    entries: list[TrashEntryWire] = Field(default_factory=list)
    next_marker: str | None = Field(default=None, serialization_alias="nextMarker")


class TrashEmptyResult(BaseModel):
    """What one sweep of the trash did, and what it left behind.

    ``skipped`` exists because emptying is decided per deletion: a root the
    caller may not delete stays, and an answer naming only what it removed reads
    as "the trash is empty" when it is not.
    """

    model_config = ConfigDict(populate_by_name=True)

    removed: int = 0
    skipped: int = 0
    #: The distinct policy codes behind the skips, not one entry per root: a
    #: sweep over thousands must not answer with thousands of lines, and a code
    #: is the reason — the names of the things left behind are a listing.
    skipped_reasons: list[str] = Field(default_factory=list, serialization_alias="skippedReasons")


class RestoreRequest(BaseModel):
    """Where a restore should land. Omit ``parentId`` for the original place."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    parent_id: UUID | None = None


def _drive(ctx: FilesContext, drive_id: UUID) -> DriveId:
    if ctx.drive.id != drive_id:
        raise NotFound()
    return DriveId(ctx.drive.id)


def _trash(ctx: FilesContext) -> Trash:
    return Trash(ctx.repo, ctx.ctx, ctx.clock, ctx.store)


def _entry_body(entry: TrashEntry, item: Item, original_path: str | None) -> TrashEntryWire:
    return TrashEntryWire(
        trash_op_id=str(entry.trash_op_id),
        original_parent_id=(
            None if entry.original_parent_id is None else str(entry.original_parent_id)
        ),
        original_path=original_path,
        deleted_at=entry.deleted_at.isoformat(),
        purge_after=entry.purge_after.isoformat(),
        time_left_seconds=int(entry.time_left.total_seconds()),
        reason=entry.reason,
        reason_machine=entry.reason_machine,
        item=item,
    )


@router.get(
    "/drives/{drive_id}/trash",
    response_model=TrashPage,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("trash"))],
)
async def list_trash(
    request: Request,
    drive_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    marker: Annotated[str | None, Query()] = None,
) -> TrashPage:
    """One page of this drive's trashed roots, filtered to what the caller sees."""
    drive = _drive(ctx, drive_id)
    facts = await facts_for(request, db, ctx.ctx, ctx.drive)
    entries: list[TrashEntryWire] = []
    # The ids each entry's lease could be held on, collected as the entries are
    # decided, so the lookup runs once for the page rather than once per entry.
    lease_ids: list[tuple[UUID, list[UUID]]] = []
    # Each entry's old location, as parts read while its rows are live; the
    # homes in them are named once for the page, after the loop.
    original_parts: list[list[PathPart] | None] = []
    async with ctx.repo.transaction():
        page = await _trash(ctx).list_trash(drive, marker=marker, limit=TRASH_PAGE_LIMIT)
        # The page is decided in one batch and filtered, the way every listing
        # is: a listing is a rendering pass, not an access attempt on every row
        # it looks past, so it records one decision for the page and never
        # stops at a row it may not read. Every id is read off the page before
        # anything is decided.
        ids = [NodeId(entry.node.id) for entry in page.entries]
        decided = await decided_page(
            request, db, ctx.repo, ctx.ctx, ids, FilesAction.READ, facts=facts
        )
        rows = await ctx.repo.node_rows(ids, starred_for=ctx.ctx.effective_user_id)
        for entry in page.entries:
            row = decided.get(entry.node.id)
            if row is None:
                continue
            loaded = rows.get(entry.node.id)
            # The folder it came from. A purged parent reads as no location
            # rather than a stale one.
            parts: list[PathPart] | None = None
            if entry.original_parent_id is not None:
                parent = await ctx.repo.node(NodeId(entry.original_parent_id))
                if parent is not None:
                    parts = path_parts(await ctx.repo.chain(parent))
            original_parts.append(parts)
            entries.append(
                _entry_body(
                    entry,
                    to_item(
                        row.node,
                        row.chain,
                        row.access,
                        None,
                        version=loaded.head if loaded is not None else None,
                        starred=loaded.starred if loaded is not None else False,
                    ),
                    None,
                )
            )
            lease_ids.append((row.node.id, [ancestor.id for ancestor in row.chain]))
        # One statement for the whole page. Without it the trash told a member
        # that a folder somebody has mounted was under no lease at all, while
        # the very same folder in the main listing said who was holding it.
        snapshots = await lease_snapshots.lease_facets(
            ctx.repo, lease_ids, ctx=ctx.ctx, now=ctx.clock.now()
        )
        # Appended in lockstep with `entries`, so the pairing is positional.
        entries = [
            wire.model_copy(update={"item": with_lease(wire.item, snapshots.get(node_id))})
            for wire, (node_id, _chain) in zip(entries, lease_ids, strict=True)
        ]
        marker_out = page.next_marker
    # One statement for the page names every home the entries' items and old
    # locations run through; `users` is a platform table, so the lookup steps
    # out of the tenant role for exactly its own statement.
    org_team_id = ctx.repo.scope.org_team_id
    owners = {owner for parts in original_parts for _name, owner in parts or () if owner}
    async with as_platform(db):
        labels = await home_labels(db, owners, org_team_id=org_team_id)
        named = await with_owner_names(db, [wire.item for wire in entries], org_team_id=org_team_id)
    # The holder named the way every listing names it: the box, the chat, and
    # whether it acts for the reader. Without it a refusal on a row here could
    # only say "someone", even about the reader's own chat.
    async with as_platform(ctx.repo.session):
        named = await with_lease_chats(ctx, request, await with_lease_machines(ctx, named))
    entries = [
        wire.model_copy(
            update={
                "item": item,
                "original_path": None if parts is None else display_path(parts, labels),
            }
        )
        for wire, item, parts in zip(entries, named, original_parts, strict=True)
    ]
    return TrashPage(entries=entries, next_marker=marker_out)


@router.post(
    "/drives/{drive_id}/trash/{op_id}/restore", dependencies=[Depends(ratelimited("trash"))]
)
@idempotent_route("files.trash.restore_from_trash")
async def restore_from_trash(
    request: Request,
    drive_id: UUID,
    op_id: UUID,
    body: RestoreRequest,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    idempotency_key: Idempotency,
    _if_match: IfMatch,
    lease: Lease,
    background: BackgroundTasks,
) -> Any:
    """Bring one deletion back, at its old place or a named one.

    The precondition names the trash op, which is an immutable record of one
    deletion and so carries no etag of its own to compare: the header is
    required because every mutation carries an ``If-Match``, with no exception, and
    the row itself is what makes the request replay-safe — a second restore of
    a settled op finds nothing to bring back rather than moving a live tree.
    """
    _drive(ctx, drive_id)
    async with ctx.repo.transaction():
        async with as_platform(db):
            facts = await facts_for(request, db, ctx.ctx, ctx.drive)
        op = await ctx.repo.trash_op(op_id)
        if op is None or op.drive_id != drive_id:
            raise NotFound()
        allowed = await authorized(
            request,
            db,
            ctx.repo,
            ctx.ctx,
            NodeId(op.root_node_id),
            FilesAction.RESTORE,
            facts=facts,
        )
        if body.parent_id is not None:
            destination = await authorized(
                request,
                db,
                ctx.repo,
                ctx.ctx,
                NodeId(body.parent_id),
                FilesAction.WRITE,
                facts=facts,
            )

            async def decide(node_id: UUID, action: FilesAction) -> Authorized[Any]:
                return await authorized(
                    request, db, ctx.repo, ctx.ctx, NodeId(node_id), action, facts=facts
                )

            # A restore aimed at a folder is a move, and the one rule every
            # move runs applies: landing a colleague's trashed folder in the
            # caller's own home would make them its owner.
            await refuse_a_move_that_raises_the_mover(allowed, destination, decide=decide)
        restored = await _trash(ctx).restore(
            TrashOpId(op_id),
            parent_id=None if body.parent_id is None else NodeId(body.parent_id),
            lease=fence(lease, ctx),
        )
        if isinstance(restored, OperationState):
            # A restore whose re-parent is too large to run inline is handed
            # back as the operation, exactly as an oversized move is: the tree
            # is not back yet, so answering with the node would say it was.
            queue_inline(background, ctx, "large_move", OperationId(restored.id))
            return queued_answer(drive_id, restored)
        chain = await ctx.repo.chain(restored)
        snapshot = await lease_snapshots.lease_facet(
            ctx.repo, restored, chain, ctx=ctx.ctx, now=ctx.clock.now()
        )
        item = to_item(restored, chain, allowed.access, snapshot)
    return item


def _refusal_code(refusal: FilesError | HTTPException) -> str:
    """The machine-readable reason a root was left behind.

    A ``FilesError`` carries the policy's own code. An ``HTTPException`` raised
    by the decision layer carries it in the body it was built with; when it does
    not, the status is the only honest thing left to say.
    """
    if isinstance(refusal, FilesError):
        return refusal.code
    detail = refusal.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str):
            return code
    return f"http_{refusal.status_code}"


@router.post(
    "/drives/{drive_id}/trash/empty",
    response_model=TrashEmptyResult,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("trash"))],
)
@idempotent_route("files.trash.empty_trash")
async def empty_trash(
    request: Request,
    drive_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    idempotency_key: Idempotency,
    _if_match: IfMatch,
    lease: Lease,
) -> TrashEmptyResult:
    """Purge the trashed roots this caller may delete. A held subtree refuses.

    The decision is per deletion rather than per drive: emptying is not a
    drive-level verb, it is "delete each of these", and a caller who may not
    delete one of them must not have it purged by asking for all of them. What
    that leaves behind is counted and reported, because the browser's notice is
    written from this answer and "removed 50" alone reads as "the trash is now
    empty".

    The whole trash is swept, not one listing page: the listing's page size is a
    rendering budget and has nothing to do with how much a person asked to
    delete. Paging is by the listing's own marker, so a refused root advances
    the cursor past itself instead of being met again forever, and a root seen
    once is never counted twice.

    The precondition names the drive root, the one node the whole sweep is
    addressed at; it is not compared against a single trashed node because the
    sweep addresses many, and one of their etags would say nothing about the
    rest. The header is required all the same, since every mutation carries
    one, and the per-entry decision above is what actually bounds the sweep.
    """
    drive = _drive(ctx, drive_id)
    removed = 0
    skipped = 0
    reasons: set[str] = set()
    seen: set[UUID] = set()
    async with ctx.repo.transaction():
        async with as_platform(db):
            facts = await facts_for(request, db, ctx.ctx, ctx.drive)
        trash = _trash(ctx)
        # A refused root's denial is that root's row, not the sweep's: it is
        # recorded in a session of its own and the sweep's transaction, with
        # the purges already made, is left where it was.
        decide = files_enforcer(request, db, wrap=platform_wrap(db))
        marker: str | None = None
        while True:
            page = await trash.list_trash(drive, marker=marker, limit=TRASH_PAGE_LIMIT)
            # Ids are read out before the first purge: a purged row's ORM
            # instance is expired, and touching it again would try to reload
            # what was just deleted.
            roots = [
                (UUID(str(entry.trash_op_id)), NodeId(entry.node.id))
                for entry in page.entries
                if UUID(str(entry.trash_op_id)) not in seen
            ]
            if not roots:
                break
            for op_id, root in roots:
                seen.add(op_id)
                try:
                    await authorize(
                        ctx.ctx,
                        ctx.repo,
                        root,
                        FilesAction.DELETE,
                        facts=facts,
                        enforce=decide,
                    )
                except (FilesError, HTTPException) as refusal:
                    skipped += 1
                    reasons.add(_refusal_code(refusal))
                    continue
                await trash.purge(root, lease=fence(lease, ctx))
                removed += 1
            marker = page.next_marker
            if marker is None:
                break
    return TrashEmptyResult(removed=removed, skipped=skipped, skipped_reasons=sorted(reasons))


__all__ = [
    "TRASH_PAGE_LIMIT",
    "RestoreRequest",
    "TrashEmptyResult",
    "empty_trash",
    "list_trash",
    "restore_from_trash",
    "router",
]
