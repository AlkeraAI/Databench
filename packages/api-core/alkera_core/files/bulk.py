"""The batch plan: what a queued bulk operation carries on its row.

A batch small enough to apply inside its request is applied there. A batch too
big to hold a transaction open for becomes an operation, and an operation is
only as resumable as what it wrote down — so the items, their per-item
``If-Match`` preconditions and the cursor the runner stopped at live on
``file_ops.result``, exactly as a copy's plan does.

Validation happens once, here, before anything is queued or applied. That is
what makes the two paths answer the same way: a batch that names no items, one
that repeats a correlation id and one that omits the field its verb needs are
refused identically whether they were small enough to run inline or large
enough to be queued, because both shapes call :func:`plan` first.

Nothing in this module authorizes. An item's node reference is carried by id
and is resolved — and refused — by whoever runs it, so a plan is never a
capability: writing one down grants nothing that reading the same ids back out
would not.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, get_args

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.errors import FilesError, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.ops import OperationCancelled, Operations, Progress
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode

#: The kind a queued batch is filed under. Filing it under the most
#: consequential verb it happens to contain would name one of the things it
#: does and hide the rest.
BULK_KIND: Final = "bulk"

Verb = Literal["createFolder", "move", "copy", "trash", "star", "unstar"]
"""The verbs a batch item may carry. Content is deliberately absent: bytes go
through a content PUT or an upload session, which have their own framing, their
own quota check and their own cap."""

VERBS: Final[frozenset[str]] = frozenset(get_args(Verb))

#: The verbs that rewrite a subtree's paths, and so hold the drive's tree
#: exclusive (:meth:`FilesRepo.lock_namespace`).
REWRITES_SUBTREE: Final[frozenset[str]] = frozenset({"move", "trash"})


async def lock_tree_for_batch(repo: FilesRepo, drive_id: DriveId, ops: list[str]) -> None:
    """Take the drive's tree in the strongest mode a batch of ``ops`` will need,
    before its first item.

    Items run one after the other in one transaction: a create takes the tree
    shared, and a move after it would ask for it exclusive while holding it
    shared, waiting on every other writer that holds it shared while they
    wait for it. A batch with any move or trash takes it exclusive up front."""
    if any(op in REWRITES_SUBTREE for op in ops):
        await repo.lock_namespace(drive_id, exclusive=True)


#: ``maxNodesPerRequest``. A batch above this is refused rather than truncated:
#: truncating would apply a prefix of what the caller asked for and answer as
#: though it had done all of it.
MAX_ITEMS: Final = 1_000

#: Which reference each verb needs. A verb whose field is missing is the
#: caller's mistake and is refused before any node is read, so an item that
#: could never have been applied never reaches a policy at all.
_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "createFolder": ("parent_id", "name"),
    "move": ("item_id", "parent_id"),
    "copy": ("item_id", "parent_id"),
    "trash": ("item_id",),
    "star": ("item_id",),
    "unstar": ("item_id",),
}


@dataclass(frozen=True, slots=True)
class BulkItemPlan:
    """One change in a batch, validated.

    ``id`` is the client's own correlation handle: results come back carrying
    it, so a caller matches answers to requests without relying on order. It is
    never an id this server issued.
    """

    id: str
    op: Verb
    item_id: NodeId | None = None
    parent_id: NodeId | None = None
    name: str | None = None
    if_match: int | None = None
    conflict: Literal["fail", "rename"] = "fail"
    allowed: bool = True
    """Did the policy allow this item when the batch was accepted?

    Recorded at request time, with the request's enforcer and facts, and never
    recomputed by the runner: the plan IS the capability, exactly as a copy's
    plan is. An item the policy refused carries ``allowed=False`` and the row
    the caller will read in :attr:`denial`, so the runner skips it without
    knowing anything about who asked."""
    denial: dict[str, Any] | None = None
    """The refusal this item earned, as ``{"status": ..., "body": ...}``."""

    def dump(self) -> dict[str, Any]:
        """The item as it is written onto the operation row."""
        body: dict[str, Any] = {"id": self.id, "op": self.op, "conflict": self.conflict}
        if not self.allowed:
            body["allowed"] = False
        if self.denial is not None:
            body["denial"] = dict(self.denial)
        if self.item_id is not None:
            body["item_id"] = str(self.item_id)
        if self.parent_id is not None:
            body["parent_id"] = str(self.parent_id)
        if self.name is not None:
            body["name"] = self.name
        if self.if_match is not None:
            body["if_match"] = self.if_match
        return body

    @classmethod
    def load(cls, body: dict[str, Any]) -> BulkItemPlan:
        """The item a runner reads back off the row."""
        raw_item = body.get("item_id")
        raw_parent = body.get("parent_id")
        raw_match = body.get("if_match")
        return cls(
            id=str(body["id"]),
            op=_verb(str(body["op"])),
            item_id=None if raw_item is None else NodeId(uuid.UUID(str(raw_item))),
            parent_id=None if raw_parent is None else NodeId(uuid.UUID(str(raw_parent))),
            name=None if body.get("name") is None else str(body["name"]),
            if_match=None if raw_match is None else int(raw_match),
            conflict="rename" if str(body.get("conflict", "fail")) == "rename" else "fail",
            allowed=bool(body.get("allowed", True)),
            denial=None if body.get("denial") is None else dict(body["denial"]),
        )


@dataclass(frozen=True, slots=True)
class BulkPlan:
    """A validated batch, plus how far a runner got through it.

    ``cursor`` is the number of items already applied, and ``results`` holds
    their answers in the order they were sent. The pair is the whole resume
    state: a runner that reads them back continues at item ``cursor`` and adds
    to what is already there, rather than re-applying a prefix whose effects
    are already in the tree.
    """

    items: tuple[BulkItemPlan, ...]
    cursor: int = 0
    results: tuple[dict[str, Any], ...] = ()

    @property
    def remaining(self) -> tuple[BulkItemPlan, ...]:
        """The items a resume still has to apply."""
        return self.items[self.cursor :]

    def dump(self) -> dict[str, Any]:
        """The row body this plan is stored as."""
        return {
            "items": [item.dump() for item in self.items],
            "cursor": self.cursor,
            "results": [dict(one) for one in self.results],
        }

    def decide(self, decisions: Mapping[str, dict[str, Any] | None]) -> BulkPlan:
        """Stamp each item with the answer the request's policy call gave it.

        ``decisions`` maps a correlation id to the refusal row that item
        earned, or to ``None`` when it was allowed. An id the mapping does not
        name is left as it was, so a caller that only reports its refusals says
        the same thing as one that reports every item.
        """
        return replace(
            self,
            items=tuple(
                item
                if item.id not in decisions
                else replace(
                    item,
                    allowed=decisions[item.id] is None,
                    denial=decisions[item.id],
                )
                for item in self.items
            ),
        )

    @classmethod
    def load(cls, body: dict[str, Any]) -> BulkPlan:
        """The plan a runner reads back off ``file_ops.result``."""
        raw = body.get("items")
        if not isinstance(raw, list):
            raise InvalidRequest("files.bulk_plan_missing", "this operation carries no batch plan")
        return cls(
            items=tuple(BulkItemPlan.load(dict(one)) for one in raw),
            cursor=int(body.get("cursor", 0)),
            results=tuple(dict(one) for one in body.get("results", [])),
        )


def plan(items: list[dict[str, Any]]) -> BulkPlan:
    """Validate a batch and return the plan both paths run from.

    Every refusal here is about the *request* rather than about the caller, so
    it is decided before a node is read: a batch of 5,000 items is the wrong
    shape whether or not its items exist, and answering it with anything a
    lookup could have influenced would be an oracle.
    """
    if not items:
        raise InvalidRequest("files.bulk_empty", "a batch names no items")
    if len(items) > MAX_ITEMS:
        raise InvalidRequest(
            "files.bulk_too_large",
            f"a batch carries at most {MAX_ITEMS} items",
            detail={"limit": MAX_ITEMS},
        )
    seen: set[str] = set()
    built: list[BulkItemPlan] = []
    for raw in items:
        correlation = str(raw.get("id", ""))
        if not correlation.strip():
            raise InvalidRequest("files.bulk_missing_field", "this item needs id")
        if correlation in seen:
            raise InvalidRequest("files.bulk_duplicate_id", "two items share one correlation id")
        seen.add(correlation)
        built.append(_item(correlation, raw))
    return BulkPlan(items=tuple(built))


def _item(correlation: str, raw: dict[str, Any]) -> BulkItemPlan:
    verb = _verb(str(raw.get("op", "")))
    for field in _REQUIRED[verb]:
        value = raw.get(field)
        if value is None or not str(value).strip():
            raise InvalidRequest("files.bulk_missing_field", f"this item needs {_wire(field)}")
    return BulkItemPlan(
        id=correlation,
        op=verb,
        item_id=_node(raw.get("item_id"), "itemId"),
        parent_id=_node(raw.get("parent_id"), "parentId"),
        name=None if raw.get("name") is None else str(raw["name"]),
        if_match=None if raw.get("if_match") is None else int(raw["if_match"]),
        conflict="rename" if str(raw.get("conflict", "fail")) == "rename" else "fail",
    )


def _verb(raw: str) -> Verb:
    if raw not in VERBS:
        raise InvalidRequest("files.bulk_bad_op", "this item names no verb this batch understands")
    verb: Verb = raw  # type: ignore[assignment]
    return verb


def _node(raw: Any, field: str) -> NodeId | None:
    if raw is None:
        return None
    try:
        return NodeId(uuid.UUID(str(raw)))
    except ValueError:
        raise InvalidRequest("files.bad_id", f"{field} is not an id this server issued") from None


def _wire(field: str) -> str:
    return {"item_id": "itemId", "parent_id": "parentId"}.get(field, field)


# ---- running a queued batch ------------------------------------------------

#: How many items one committed batch applies. Small enough that a killed
#: runner loses at most this many items' worth of work and re-does none of it,
#: large enough that a 1,000-item batch is twenty commits rather than a
#: thousand.
BULK_BATCH: Final = 50

#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()


async def run(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = BULK_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> None:
    """Run - or resume - the batch recorded on ``op_id``.

    Resumable by construction: every batch writes its results and its cursor in
    the same transaction as the changes it made, and the body re-reads the plan
    from the row each time round. A runner killed between two batches therefore
    continues at the boundary; one killed inside a batch loses that batch whole,
    because nothing it did was committed.

    Nothing here authorizes. The plan already carries the decision the request
    took with the request's own facts, so an item marked refused becomes its
    recorded row and an item marked allowed runs - re-deciding it now, in a
    process that is not the caller's, would be deciding on different facts.
    """
    ops = Operations(repo, ctx, clock if clock is not None else SystemClock())

    async def body(progress: Progress) -> None:
        await _drive(repo, ctx, op_id, progress, batch=batch, clock=clock, checkpoints=checkpoints)

    await ops.run(op_id, body)


async def resume(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = BULK_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> str:
    """Continue a batch whose runner died, and report the terminal state.

    A batch that already finished reports what it reached rather than refusing:
    a lost activity completion is not a reason to fail work that happened. The
    row goes back to ``queued`` first so the ordinary claim applies, which is
    what keeps one runner per operation whether it is the first or the fifth.
    """
    ops = Operations(repo, ctx, clock if clock is not None else SystemClock())
    state = await ops.get(op_id)
    if state.state in ("done", "cancelled"):
        return state.state
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'queued' "
                "WHERE id = :id AND org_team_id = :org AND state IN ('running', 'failed')"
            ),
            {"id": op_id, "org": repo.scope.org_team_id},
        )
    await run(repo, ctx, op_id, batch=batch, clock=clock, checkpoints=checkpoints)
    return (await ops.get(op_id)).state


async def load_bulk_plan(repo: FilesRepo, op_id: OperationId) -> BulkPlan:
    """Read the batch and its cursor off the operation row."""
    async with repo.transaction():
        row = (
            await repo.session.execute(
                text(
                    "SELECT result FROM file_ops "
                    "WHERE id = :id AND org_team_id = :org AND kind = :kind"
                ),
                {"id": op_id, "org": repo.scope.org_team_id, "kind": BULK_KIND},
            )
        ).first()
    if row is None or not row[0]:
        raise NotFound(f"no batch {op_id}")
    return BulkPlan.load(dict(row[0]))


async def store_plan(repo: FilesRepo, op_id: OperationId, current: BulkPlan) -> None:
    """Write the batch, its cursor and its results back onto the row."""
    await repo.session.execute(
        text(
            "UPDATE file_ops SET result = CAST(:body AS jsonb) "
            "WHERE id = :id AND org_team_id = :org"
        ),
        {"id": op_id, "org": repo.scope.org_team_id, "body": json.dumps(current.dump())},
    )


async def _drive(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    progress: Progress,
    *,
    batch: int,
    clock: Clock | None,
    checkpoints: Checkpoints,
) -> None:
    current = await load_bulk_plan(repo, op_id)
    drive_id = (await Operations(repo, ctx, clock or SystemClock()).get(op_id)).drive_id
    while True:
        window = current.remaining[:batch]
        if not window:
            break
        applied = await _run_batch(repo, ctx, op_id, drive_id, current, window, clock=clock)
        await progress.tick(len(window))
        await checkpoints.reach("bulk.after_batch")
        current = await load_bulk_plan(repo, op_id)
        if current.cursor < applied.cursor:
            # The row is the only record of how far this got, so a row that did
            # not move is a batch this loop would apply forever. Failing names
            # the problem; spinning would hide it behind a watchdog timeout.
            raise PreconditionFailed(f"batch {op_id} did not advance past item {current.cursor}")
        # Every committed batch is a whole set of applied items, so a cancel is
        # honoured at the boundary with nothing to drain - and the results
        # already on the row are exactly what the caller is told happened.
        if await progress.cancelled():
            raise OperationCancelled(str(op_id))


async def _run_batch(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    drive_id: DriveId,
    current: BulkPlan,
    window: tuple[BulkItemPlan, ...],
    *,
    clock: Clock | None,
) -> BulkPlan:
    """Apply one window of items and advance the cursor, in one transaction."""
    made: list[dict[str, Any]] = []
    async with repo.transaction():
        await lock_tree_for_batch(repo, drive_id, [item.op for item in window if item.allowed])
        for item in window:
            made.append(await _apply(repo, ctx, item, clock=clock))
        advanced = replace(
            current,
            cursor=current.cursor + len(window),
            results=(*current.results, *made),
        )
        await store_plan(repo, op_id, advanced)
    return advanced


async def _apply(
    repo: FilesRepo, ctx: ActingContext, item: BulkItemPlan, *, clock: Clock | None
) -> dict[str, Any]:
    """One item, inside its own savepoint, as the row the caller will read.

    The savepoint is what makes a per-item result honest: an item that raises
    undoes only itself, so the items around it are neither rolled back by its
    failure nor left depending on it.
    """
    if not item.allowed:
        # Decided in the request, with the request's facts. The decision row was
        # written then; this is only the caller's copy of the answer.
        refusal = item.denial or {"status": 404, "body": {"code": "not_found"}}
        return {"id": item.id, **refusal}
    savepoint = repo.session.begin_nested()
    await savepoint.start()
    try:
        status, body = await _perform(repo, ctx, item, clock=clock)
    except FilesError as exc:
        if savepoint.is_active:
            await savepoint.rollback()
        return {"id": item.id, "status": exc.status, "body": {"code": exc.code}}
    if savepoint.is_active:
        await savepoint.commit()
    return {"id": item.id, "status": status, "body": body}


async def _perform(
    repo: FilesRepo, ctx: ActingContext, item: BulkItemPlan, *, clock: Clock | None
) -> tuple[int, dict[str, Any] | None]:
    """Run one allowed item through the same library verb its own route calls.

    Ids only in the body: a batch's results are read back by whoever polls the
    operation, and a name in there would be a name this server hands out
    without deciding, at poll time, whether it may.
    """
    from alkera_core.files import stars
    from alkera_core.files.copy import start_copy
    from alkera_core.files.namespace import Namespace
    from alkera_core.files.trash import Trash

    ticking = clock if clock is not None else SystemClock()
    if item.op == "createFolder":
        parent = await _require_node(repo, _required(item.parent_id, "parentId"))
        node = await Namespace(repo, ctx, ticking).create(
            DriveId(parent.drive_id),
            NodeId(parent.id),
            "folder",
            str(item.name or "").encode(),
            conflict=item.conflict,
        )
        return 201, {"id": str(node.id)}
    if item.op == "move":
        moved = await Namespace(repo, ctx, ticking).move(
            _required(item.item_id, "itemId"),
            _required(item.parent_id, "parentId"),
            if_match=_required_if_match(item),
            conflict=item.conflict,
        )
        return 200, {"id": str(moved.id)}
    if item.op == "copy":
        source = await _require_node(repo, _required(item.item_id, "itemId"))
        destination = await _require_node(repo, _required(item.parent_id, "parentId"))
        queued = await start_copy(
            repo,
            Operations(repo, ctx, ticking),
            node=source,
            dest_parent=destination,
        )
        return 202, {"id": str(queued)}
    if item.op == "trash":
        await Trash(repo, ctx, ticking).trash(
            _required(item.item_id, "itemId"), if_match=_required_if_match(item)
        )
        return 204, None
    node_id = _required(item.item_id, "itemId")
    if item.op == "star":
        await stars.star(repo, ctx, node_id)
    else:
        await stars.unstar(repo, ctx, node_id)
    return 200, {"id": str(node_id)}


async def _require_node(repo: FilesRepo, node_id: NodeId) -> FileNode:
    node = await repo.node(node_id)
    if node is None:
        raise NotFound(f"no node {node_id}")
    return node


def _required(value: NodeId | None, field: str) -> NodeId:
    if value is None:  # pragma: no cover - plan refuses an item without its field
        raise InvalidRequest("files.bulk_missing_field", f"this item needs {field}")
    return value


def _required_if_match(item: BulkItemPlan) -> int:
    if item.if_match is None:
        raise InvalidRequest("files.if_match_required", "this item requires ifMatch")
    return item.if_match


__all__ = [
    "BULK_BATCH",
    "BULK_KIND",
    "MAX_ITEMS",
    "VERBS",
    "BulkItemPlan",
    "BulkPlan",
    "Verb",
    "load_bulk_plan",
    "plan",
    "resume",
    "run",
    "store_plan",
]
