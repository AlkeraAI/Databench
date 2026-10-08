"""The batch route: many small tree changes in one request, one answer each.

A client that has just dropped a folder, emptied a selection or starred a page
of results holds tens or hundreds of one-item calls. Sending them one by one
costs a round trip and a rate-limit slot each, and — worse — leaves the tree in
whatever state the network happened to reach. This route takes the whole batch,
runs it as ONE Files transaction, and answers with one row per item.

Three properties are the contract, and each has a test:

* **Never a partial silent success.** Every item gets its own ``status`` and,
  on a refusal, the same ``code`` body the single-item route would have sent.
  A batch that half-applied says so item by item; nothing is swallowed.
* **The same authorization as the single-item routes.** Each item is resolved
  through the same helper the item routes use, so a stranger's node, a node in
  another org and a node that does not exist all reach the same ``enforce``
  call and leave the same opaque ``not_found`` row — while the caller's own
  items proceed.
* **Each item carries its own ``If-Match``.** A stale etag on one item is that
  item's 412; it does not decide anything about its neighbours.

Sizing: a batch over :data:`INLINE_ITEMS` is not a request, it is work — it
becomes a queued operation and answers 202, exactly as a copy does, so a client
writes one progress path rather than two. A batch over :data:`MAX_ITEMS` is
refused outright; the 64 MiB body cap in ``backend.api.body_limit`` bounds the
bytes before any of this runs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from alkera_core.files import bulk as bulk_core
from alkera_core.files import lease_snapshots, stars
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Denied
from alkera_core.files.authz.decider import EffectiveAccess
from alkera_core.files.errors import FilesError, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.leases import fenced_write_for
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import Operations, OperationState
from alkera_core.files.trash import Trash
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.files.item import Item
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import text

from backend.api.deps.files import (
    Idempotency,
    Lease,
    LeaseContext,
    as_platform,
    idempotent_route,
    ratelimited,
    restamp,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_errors import body_for
from backend.api.deps.files_facts import facts_for
from backend.api.deps.files_nodes import (
    _fence,
    _name_bytes,
    _resolve,
    _uuid,
    files_txn,
)
from backend.api.params import PathId
from backend.api.routes.files.operations import OperationWire
from backend.authz.enforce import OutboxDecisionSink
from backend.services.files.context import FilesContext
from backend.services.files.guards import (
    Decide,
    refuse_a_copy_over_hidden_chats,
    refuse_a_move_that_raises_the_mover,
)
from backend.services.files.items import to_item, with_lease
from backend.services.files.operations_runner import queue_inline
from backend.services.realtime import flush_many_before_trash

router = APIRouter()

#: The verbs a batch item may carry. Content is deliberately absent: bytes go
#: through a content PUT or an upload session, which have their own framing,
#: their own quota check and their own cap.
Verb = Literal["createFolder", "move", "copy", "trash", "star", "unstar"]

#: ``maxNodesPerRequest``, owned by the library so the inline path and the
#: queued path cannot disagree about which batches are too big to accept.
MAX_ITEMS: Final = bulk_core.MAX_ITEMS

#: Above this the batch is queued as an operation instead of being applied in
#: the request. Below it the whole batch is one transaction, so the round trip
#: a client saves is not paid back in a half-applied tree.
INLINE_ITEMS: Final = 100

#: A queued batch is its own ``file_ops`` kind. Filing it under the most
#: consequential verb it happens to contain would name one of the things it
#: does and hide the rest, and would point undo at that verb's inverse rather
#: than at the batch's own.
_OP_KIND: Final = bulk_core.BULK_KIND


class BulkItem(BaseModel):
    """One change in a batch.

    ``id`` is the client's own correlation handle: the results come back
    carrying it, so a caller matches answers to requests without relying on
    order. It is never an id this server issued.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    op: Verb
    item_id: str | None = None
    parent_id: str | None = None
    name: str | None = None
    if_match: int | None = None
    conflict_behavior: Literal["fail", "rename"] = "fail"


class BulkRequest(BaseModel):
    """The batch body."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[BulkItem] = Field(default_factory=list)


class BulkItemResult(BaseModel):
    """What one item did, or why it did not.

    ``status`` is the status the same change would have carried as its own
    request, and ``body`` is the body that request would have returned — so a
    client reuses one error path for both shapes.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    status: int
    body: dict[str, Any] | None = None


class BulkResponse(BaseModel):
    """The inline answer: one row per item, in the order they were sent."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    responses: list[BulkItemResult]


async def trash_fenced(request: Request, files: FilesCtx) -> dict[str, int]:
    """The etag each trash in the batch is fenced on, by item id, once the
    live edits under every node it trashes are written back (as a single
    trash's ``If-Match`` dependency does). A dependency, so it runs before the
    route's idempotency claim and the batch's Files transaction open: the
    write backs take the files' rows in transactions of their own. A body that
    does not parse is left to the route's own validation."""
    try:
        parsed = BulkRequest.model_validate_json(await request.body())
    except ValidationError:
        return {}
    named: dict[uuid.UUID, int] = {}
    for item in parsed.items:
        if item.op != "trash" or item.item_id is None or item.if_match is None:
            continue
        try:
            named[uuid.UUID(item.item_id)] = item.if_match
        except ValueError:
            continue
    if not named:
        return {}
    fenced = await flush_many_before_trash(request.app, files.repo.scope.org_team_id, named)
    return {str(node_id): etag for node_id, etag in fenced.items() if etag != named[node_id]}


@router.post(
    "/drives/{drive_id}/bulk",
    response_model=None,
    dependencies=[Depends(ratelimited("bulk"))],
)
@idempotent_route("files.bulk.bulk")
async def bulk(
    request: Request,
    files: FilesCtx,
    drive_id: PathId,
    body: BulkRequest,
    idempotency: Idempotency,
    response: Response,
    lease: Lease,
    background: BackgroundTasks,
    flushed: Annotated[dict[str, int], Depends(trash_fenced)],
) -> BulkResponse | OperationWire:
    """Apply a batch of tree changes, or queue it when it is too big to apply.

    The size decision happens before anything is authorized, because it is
    about the request and not about the caller: a batch of 5,000 items is the
    wrong shape whether or not its items exist.
    """
    drive = _require_drive(files, _uuid(drive_id, "drive"))
    # A trash fenced on the etag its own write back produced, so the caller's
    # etag still holds after the live edits under it were saved first.
    items = [
        item.model_copy(update={"if_match": flushed[item.item_id]})
        if item.op == "trash" and item.item_id in flushed
        else item
        for item in body.items
    ]
    plan = bulk_core.plan([_plan_item(item) for item in items])

    if len(items) > INLINE_ITEMS:
        async with files_txn(files):
            operations = Operations(files.repo, files.ctx, files.clock, files.store)
            state = await operations.start(_OP_KIND, drive_id=DriveId(drive), total=len(body.items))
            # Decided HERE, with this request's enforcer and facts, and written
            # onto the row the runner reads: the plan is the capability. A batch
            # queued undecided would be a runner deciding on its own facts --
            # in a process that is not the caller's -- or, worse, not deciding
            # at all and applying every item the body happened to name.
            decided = plan.decide(await _decide_each(request, files, drive, items))
            await _store_plan(files, state, decided)
        # The row and its plan are durable; now name who runs them. Without
        # this the 202 was the whole of it: the batch sat in `queued`, nothing
        # was trashed or moved, and the client polled a row no process owned.
        queue_inline(background, files, "bulk", OperationId(state.id))
        response.status_code = 202
        return _operation_wire(state)

    results: list[BulkItemResult] = []
    # What each rendered row's lease could be held on, collected as the rows are
    # made: an item that refuses rolls its savepoint back, and one lookup per
    # item would be one statement per item for an answer the whole batch shares.
    pending: list[_Rendered] = []
    async with files_txn(files):
        await bulk_core.lock_tree_for_batch(files.repo, DriveId(drive), [item.op for item in items])
        for item in items:
            results.append(await _run_one(request, files, drive, item, pending, lease))
        # One statement for the whole batch. Without it a batch answered that a
        # folder somebody has mounted was under no lease, while the same folder
        # in the main listing named its holder.
        snapshots = await lease_snapshots.lease_facets(
            files.repo,
            [(node_id, chain_ids) for _, node_id, chain_ids, _ in pending],
            ctx=files.ctx,
            now=files.clock.now(),
        )
    leased = {
        result_id: _wire(with_lease(rendered, snapshots.get(node_id)))
        for result_id, node_id, _chain_ids, rendered in pending
    }
    return BulkResponse(
        responses=[
            row if row.id not in leased else row.model_copy(update={"body": leased[row.id]})
            for row in results
        ]
    )


#: One answered row that carries an item: the bulk item's id, the node it
#: rendered, the ids its lease could be held on, and the item itself — the lease
#: is folded in once the whole batch is decided.
_Rendered = tuple[str, uuid.UUID, list[uuid.UUID], Item]


def _operation_wire(state: OperationState) -> OperationWire:
    return OperationWire(
        id=str(state.id),
        drive_id=str(state.drive_id),
        kind=state.kind,
        state=state.state,
        done=state.done,
        total=state.total or 0,
    )


def _require_drive(files: FilesContext, drive: uuid.UUID) -> uuid.UUID:
    """A drive id that is not this org's drive is simply not there.

    Decided before the body is validated, and by raising the opaque
    ``not_found`` a drive that exists nowhere raises: otherwise a stranger who
    posts a malformed batch at a drive id learns from the 422 that the drive is
    real, which is exactly the existence oracle the opaque 404 exists to close.
    """
    if files.drive.id != drive:
        raise NotFound()
    return drive


def _plan_item(item: BulkItem) -> dict[str, Any]:
    """One wire item as the library's validator reads it."""
    return {
        "id": item.id,
        "op": item.op,
        "item_id": item.item_id,
        "parent_id": item.parent_id,
        "name": item.name,
        "if_match": item.if_match,
        "conflict": item.conflict_behavior,
    }


async def _store_plan(files: FilesContext, state: OperationState, plan: bulk_core.BulkPlan) -> None:
    """Write the batch onto the operation row that will run it.

    In the same transaction as the row, so an operation a runner picks up
    always carries its plan: a queued batch with nothing to run from is a row
    that can only ever reach ``failed``.
    """
    await files.repo.session.execute(
        text(
            "UPDATE file_ops SET result = CAST(:body AS jsonb) "
            "WHERE id = :id AND org_team_id = :org"
        ),
        {"id": state.id, "org": files.repo.scope.org_team_id, "body": json.dumps(plan.dump())},
    )


#: Which node references each verb decides, in the order the verb needs them.
#: One table for both paths: the queued branch decides an item exactly as the
#: inline branch does, so a batch cannot change what it is allowed to do by
#: being one item longer.
_REFERENCES: Final[dict[str, tuple[tuple[str, str, FilesAction], ...]]] = {
    "createFolder": (("parent_id", "parentId", FilesAction.WRITE),),
    # A write on both ends, then the shared move rule (`_guard`): a
    # move whose destination would hand the mover a rung above the one they
    # hold on the source is decided again as DELETE on the source, exactly as
    # `items.patch_item` decides it.
    "move": (
        ("item_id", "itemId", FilesAction.WRITE),
        ("parent_id", "parentId", FilesAction.WRITE),
    ),
    # COPY on the source, as `items.copy_item` and `duplicate_item` decide it,
    # so the policy's copy-only refusals apply to a batch too.
    "copy": (("item_id", "itemId", FilesAction.COPY), ("parent_id", "parentId", FilesAction.WRITE)),
    # The reversible bin, decided on the writer rung beside rename and move
    # — the same action `items.delete_item` decides its non-permanent branch
    # at. `DELETE` is the owner rung and means the purge, which this verb
    # never performs; deciding it here made a writer who can bin a file one
    # at a time refused for binning the same file in a batch.
    "trash": (("item_id", "itemId", FilesAction.WRITE),),
    # A star is the caller marking their own view of a node, so it is decided
    # as READ — bookmarking something you may read but not change is exactly
    # what it is for.
    "star": (("item_id", "itemId", FilesAction.READ),),
    "unstar": (("item_id", "itemId", FilesAction.READ),),
}


def _references(item: BulkItem) -> tuple[tuple[str | None, str, FilesAction], ...]:
    """The (id, field, action) triples this item must be allowed before it runs."""
    return tuple(
        (getattr(item, attribute), field, action)
        for attribute, field, action in _REFERENCES[item.op]
    )


async def _decide_each(
    request: Request, files: FilesContext, drive: uuid.UUID, items: list[BulkItem]
) -> dict[str, dict[str, Any] | None]:
    """Decide every item of a batch that is about to be queued.

    At request time, with the request's own enforcer and facts, and with the
    same sink the inline path uses — so each refusal's DENY row is written in a
    committed session of its own and the batch's queueing transaction, and
    therefore the operation row itself, is left alone.
    """
    return {item.id: await _decide_one(request, files, drive, item) for item in items}


async def _decide_one(
    request: Request, files: FilesContext, drive: uuid.UUID, item: BulkItem
) -> dict[str, Any] | None:
    """One item's decision: ``None`` when allowed, else the row it earned.

    Only the policy runs here — nothing is applied, because a batch this size
    is queued precisely so its work does not ride the request. The savepoint is
    what keeps a refused item from taking its neighbours' decisions with it.
    """
    savepoint = files.repo.session.begin_nested()
    await savepoint.start()
    try:
        decided = [
            await _authorize(request, files, drive, raw_id, field, action)
            for raw_id, field, action in _references(item)
        ]
        await _guard(request, files, drive, item, decided)
    except FilesError as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        await _restamp(files)
        return _denial_row(exc)
    except HTTPException as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        await _restamp(files)
        return _denial_row(_as_files_error(exc))
    if savepoint.is_active:
        await savepoint.commit()
    return None


def _denial_row(exc: FilesError) -> dict[str, Any]:
    """One refusal as the row a caller reads back, inline or off the plan."""
    return {
        "status": exc.status,
        "body": body_for(exc, may_read_named_node=bool(getattr(exc, "may_read_named_node", False))),
    }


def _as_files_error(exc: HTTPException) -> FilesError:
    """``enforce``'s refusal as the Files failure of the same status.

    ``enforce`` refuses by raising, and its exception is the *request's* answer
    — which inside a batch would make one item's denial everybody else's. Any
    other status is a bug in this server rather than one item's 4xx, so it is
    re-raised untouched.
    """
    if exc.status_code not in (403, 404):
        raise exc
    return NotFound() if exc.status_code == 404 else Denied(str(exc.detail or "Not allowed"))


async def _run_one(
    request: Request,
    files: FilesContext,
    drive: uuid.UUID,
    item: BulkItem,
    pending: list[_Rendered],
    lease: LeaseContext,
) -> BulkItemResult:
    """One item, inside its own savepoint.

    The savepoint is what makes a per-item result honest: an item that raises
    undoes only itself, so the rows around it are neither rolled back by its
    failure nor left depending on it. A non-Files exception is *not* caught —
    a bug in this server is not one item's 4xx.
    """
    savepoint = files.repo.session.begin_nested()
    await savepoint.start()
    try:
        status, payload = await _apply(request, files, drive, item, pending, lease)
    except FilesError as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        await _restamp(files)
        row = _denial_row(exc)
        return BulkItemResult(id=item.id, status=int(row["status"]), body=row["body"])
    except HTTPException as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        await _restamp(files)
        row = _denial_row(_as_files_error(exc))
        return BulkItemResult(id=item.id, status=int(row["status"]), body=row["body"])
    if savepoint.is_active:
        await savepoint.commit()
    return BulkItemResult(id=item.id, status=status, body=payload)


def _record(
    pending: list[_Rendered],
    result_id: str,
    node: FileNode,
    chain: Sequence[FileNode],
    access: EffectiveAccess,
) -> Item:
    """Render one row and remember what its lease could be held on.

    The ids are read here, while the row is live: a later item's refusal rolls
    its savepoint back and expires these objects, and the batched lookup only
    runs once every item has been answered.
    """
    rendered = to_item(node, chain, access)
    pending.append((result_id, node.id, [ancestor.id for ancestor in chain], rendered))
    return rendered


async def _restamp(files: FilesContext) -> None:
    """Re-apply the tenant stamp after this item's savepoint was rolled back.

    The same repair every refusal needs, so it is the same helper: a batch
    reaches it per item, and a single-decision route reaches it through
    ``as_platform``'s way out.
    """
    await restamp(files.repo.session, str(files.repo.scope.org_team_id))


async def _apply(
    request: Request,
    files: FilesContext,
    drive: uuid.UUID,
    item: BulkItem,
    pending: list[_Rendered],
    lease: LeaseContext,
) -> tuple[int, dict[str, Any] | None]:
    """Perform one item and return the status and body it earned."""
    references = _references(item)
    if item.op == "createFolder":
        parent = await _authorize(request, files, drive, *references[0])
        _require_etag(item, parent)
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        # Fenced like the single-item create it batches. The library refuses an
        # unfenced create inside a mount on its own, so leaving the lease out
        # was fail-closed — but it closed on the holder too, and a batch that
        # cannot say whose write it is cannot be the holder's.
        node = await namespace.create(
            DriveId(drive),
            NodeId(parent.node.id),
            "folder",
            _name_bytes(_require(item.name, "name")),
            conflict=item.conflict_behavior,
            lease=_fence(lease, files),
        )
        return 201, _wire(
            _record(pending, item.id, node, [*parent.chain, parent.node], parent.access)
        )

    if item.op == "move":
        source = await _authorize(request, files, drive, *references[0])
        target = await _authorize(request, files, drive, *references[1])
        await _guard(request, files, drive, item, (source, target))
        namespace = Namespace(
            files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
        )
        moved = await namespace.move(
            NodeId(source.node.id),
            NodeId(target.node.id),
            if_match=_required_if_match(item),
            conflict=item.conflict_behavior,
            lease=_fence(lease, files),
        )
        if isinstance(moved, OperationState):
            return 202, _wire(_operation_wire(moved))
        chain = await files.repo.chain(moved)
        return 200, _wire(_record(pending, item.id, moved, chain, source.access))

    if item.op == "copy":
        origin = await _authorize(request, files, drive, *references[0])
        destination = await _authorize(request, files, drive, *references[1])
        await _guard(request, files, drive, item, (origin, destination))
        # Decided before the item is queued, because after this the copy belongs
        # to a runner the request cannot reach: a batch was otherwise a way to
        # plant a whole tree inside a mount that the same copy, sent on its own,
        # is refused.
        await fenced_write_for(files.repo, destination.node, _fence(lease, files), into=True)
        # A copy inside a batch is queued as the BATCH's own kind, carrying just
        # this item: an operation filed as ``copy`` would point undo at a copy's
        # inverse rather than at the batch's, and the runner that picks it up is
        # the bulk runner, which is what actually holds the per-item decision.
        operations = Operations(files.repo, files.ctx, files.clock, files.store)
        state = await operations.start(_OP_KIND, drive_id=DriveId(origin.node.drive_id), total=1)
        await _store_plan(files, state, bulk_core.plan([_plan_item(item)]))
        return 202, _wire(_operation_wire(state))

    if item.op == "trash":
        doomed = await _authorize(request, files, drive, *references[0])
        trash = Trash(files.repo, files.ctx, files.clock, files.store)
        # Fenced like the single-item delete it batches. Without the fence the
        # holder's own cleanup of its own mount was refused as someone else's
        # write, so the one caller that is allowed to trash inside a leased
        # folder was the one caller a batch could not do it from.
        await trash.trash(
            NodeId(doomed.node.id),
            if_match=_required_if_match(item),
            lease=_fence(lease, files),
        )
        return 204, None

    # star and unstar, decided as READ by the table above.
    marked = await _authorize(request, files, drive, *references[0])
    _require_etag(item, marked)
    if item.op == "star":
        await stars.star(files.repo, files.ctx, NodeId(marked.node.id))
    else:
        await stars.unstar(files.repo, files.ctx, NodeId(marked.node.id))
    await files.repo.session.refresh(marked.node)
    return 200, _wire(_record(pending, item.id, marked.node, marked.chain, marked.access))


def _by_id(request: Request, files: FilesContext, drive: uuid.UUID) -> Decide:
    """This batch's by-id door for the shared guards: ``_authorize`` itself,
    so a node a guard decides earns the item's own denial row."""

    async def decide(node_id: uuid.UUID, action: FilesAction) -> Authorized[Any]:
        return await _authorize(request, files, drive, str(node_id), "itemId", action)

    return decide


async def _guard(
    request: Request,
    files: FilesContext,
    drive: uuid.UUID,
    item: BulkItem,
    decided: Sequence[Authorized[Any]],
) -> None:
    """The rules a verb runs once both of its references are decided.

    A move that would hand the mover a rung they do not hold on the source is
    decided again as ``DELETE`` on it; a copy whose subtree holds a chat folder
    the copier cannot open is refused whole. Both are the single-item routes'
    rules, spelled once in the Files service layer, so the batch cannot admit
    what the same request sent on its own is refused.
    """
    if item.op == "move":
        await refuse_a_move_that_raises_the_mover(
            decided[0], decided[1], decide=_by_id(request, files, drive)
        )
    elif item.op == "copy":
        async with as_platform(files.repo.session):
            facts = await facts_for(request, files.repo.session, files.ctx, files.drive)
        await refuse_a_copy_over_hidden_chats(
            files.repo,
            files.ctx,
            decided[0].node,
            facts=facts,
            decide=_by_id(request, files, drive),
            action=FilesAction.COPY,
        )


async def _authorize(
    request: Request,
    files: FilesContext,
    drive: uuid.UUID,
    raw_id: str | None,
    field: str,
    action: FilesAction,
) -> Authorized[Any]:
    """Resolve one of the item's node references through the policy.

    An absent reference is the caller's mistake and is a 422 before anything is
    read; a present one goes through exactly the call the single-item routes
    make, which is what makes a stranger's node in a batch the same opaque 404
    it would be on its own.
    """
    return await _resolve(
        request,
        files,
        drive,
        _uuid(_require(raw_id, field), field),
        action,
        # An item's denial is that item's row, not the batch's: the sink writes
        # it in a committed session of its own and leaves the request's
        # transaction — and therefore every item that already applied — alone.
        sink=OutboxDecisionSink(),
    )


def _require(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise InvalidRequest("files.bulk_missing_field", f"this item needs {field}")
    return value


def _required_if_match(item: BulkItem) -> int:
    """The item's own etag, which a mutation may not omit."""
    if item.if_match is None:
        raise InvalidRequest("files.if_match_required", "this item requires ifMatch")
    return item.if_match


def _require_etag(item: BulkItem, decided: Authorized[Any]) -> None:
    """The etag check for the verbs whose library call does not take one.

    Creating a child and starring a node both leave the addressed row's etag
    where it was, so there is no statement for the precondition to ride inside;
    it is compared here instead, against the row this transaction just read.
    """
    if item.if_match is None:
        raise InvalidRequest("files.if_match_required", "this item requires ifMatch")
    if decided.node.etag != item.if_match:
        raise PreconditionFailed(f"node {decided.node.id} is not at etag {item.if_match}")


def _wire(model: BaseModel) -> dict[str, Any]:
    """A nested model as the JSON a client would have received on its own."""
    return model.model_dump(mode="json", by_alias=True)


__all__ = [
    "INLINE_ITEMS",
    "MAX_ITEMS",
    "BulkItem",
    "BulkRequest",
    "BulkResponse",
    "router",
]
