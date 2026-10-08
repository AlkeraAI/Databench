"""A move too large for a request: the same move, run as an operation.

Three things make a 20,000-node move safe to run outside a request.

*The subtree announces itself.* Before anything moves, every node at or under
the root is stamped ``state = 'moving'`` in one statement, and every request
path that would change the tree calls
:func:`~alkera_core.files.namespace.assert_not_moving` first. A writer inside
the subtree therefore gets a ``409 files.moving`` instead of racing a rewrite
that is going to overwrite its ``path_ids`` anyway, and a second move that
overlaps the subtree — in either direction along the chain — is refused before
it takes a single lock.

*Every batch is a commit, and the cursor is on the operation row.* The paths
are rewritten in id-ordered batches, each in its own transaction, with the id
the batch stopped at written into ``file_ops.result`` in that same transaction.
A crash therefore loses nothing: the resume reads the cursor and continues,
never restarts, and the batches already committed are exactly as the
uninterrupted move would have left them.

*A cancel that arrives after the root has moved drains rather than stops.* The
descendants' paths are derived from the root's, so a half-rewritten subtree is
not a tree anyone can read — ``path_ids`` would disagree with the parent walk
for every row the loop had not reached. A cancel before the root moves stops
outright and leaves the old tree; one after it finishes the rewrite and then
reports ``cancelled``. Either way the tree the caller sees is consistent and no
node is left ``moving``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl, history
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.errors import Conflict, NotFound, PreconditionFailed
from alkera_core.files.ids import MIN_UUID, DriveId, NodeId, OperationId
from alkera_core.files.ops import OperationCancelled, Operations, Progress
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.tree import FileNode

#: How many rows one batch rewrites: large enough that a
#: 20,000-node move is two statements, small enough that neither holds its
#: locks for longer than the watchdog's deadline.
MOVE_BATCH: Final = 10_000

#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()


class LargeMovePlan:
    """What the routing wrote onto the operation row for the runner to pick up."""

    __slots__ = ("cursor", "if_match", "new_parent_id", "new_path", "node_id", "old_path")

    def __init__(self, body: dict[str, Any]) -> None:
        self.node_id = NodeId(uuid.UUID(str(body["node_id"])))
        self.new_parent_id = NodeId(uuid.UUID(str(body["new_parent_id"])))
        self.if_match = int(body["if_match"])
        self.old_path: str = str(body["old_path"])
        raw_new = body.get("new_path")
        self.new_path: str | None = None if raw_new is None else str(raw_new)
        raw_cursor = body.get("cursor")
        self.cursor: str | None = None if raw_cursor is None else str(raw_cursor)

    @property
    def root_moved(self) -> bool:
        """Has the root's own row been re-parented yet?

        The new path is what the batches derive from, so its presence — not a
        separate boolean — is the truthful record of how far this got.
        """
        return self.new_path is not None

    def dump(self) -> dict[str, Any]:
        """The row body this plan came from, ready to be written back."""
        body = plan_body(
            self.node_id, self.new_parent_id, if_match=self.if_match, old_path=self.old_path
        )
        if self.new_path is not None:
            body["new_path"] = self.new_path
        if self.cursor is not None:
            body["cursor"] = self.cursor
        return body

    @property
    def depth_delta(self) -> int:
        """How far every descendant moved, derived from the two root paths."""
        if self.new_path is None:  # pragma: no cover - the batch loop needs the new path
            raise PreconditionFailed("the root has not moved yet")
        return self.new_path.count(".") - self.old_path.count(".")


def plan_body(
    node_id: NodeId, new_parent_id: NodeId, *, if_match: int, old_path: str
) -> dict[str, Any]:
    """The plan a routed move stores on its operation row."""
    return {
        "node_id": str(node_id),
        "new_parent_id": str(new_parent_id),
        "if_match": if_match,
        "old_path": old_path,
    }


async def run_large_move(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = MOVE_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> None:
    """Run — or resume — the batched move recorded on ``op_id``.

    Resumable by construction: the body reads its plan from the row every time
    round, so a second call after a crash picks up at the cursor rather than at
    the beginning. ``Operations.run`` is what refuses to start the body twice
    concurrently, because its ``queued → running`` claim is a compare-and-swap.
    """
    ops = Operations(repo, ctx, clock if clock is not None else SystemClock())

    async def body(progress: Progress) -> None:
        await _drive(repo, ctx, op_id, progress, batch=batch, checkpoints=checkpoints)

    await ops.run(op_id, body)


async def resume_large_move(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = MOVE_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> None:
    """Continue a move whose runner died, from the cursor it left behind.

    The watchdog moves a heartbeat-less operation to ``failed``; a crashed
    runner leaves it ``running`` with a stale heartbeat. Either way the resume
    puts the row back to ``queued`` so the ordinary claim applies — that is
    what keeps one runner per operation whether it is the first or the fifth.
    """
    state = await Operations(repo, ctx, clock if clock is not None else SystemClock()).get(op_id)
    if state.state in ("done", "cancelled"):
        raise PreconditionFailed(f"operation {op_id} is {state.state}; there is nothing to resume")
    plan = await load_plan(repo, op_id)
    if not await cursor_matches_rows(repo, plan):
        # The cursor names a row that is in neither half of the subtree, so
        # continuing from it would skip every row ordered before it and report
        # success over a half-rewritten tree. This move cannot be resumed from
        # its own record; the consistency check decides what is left of it.
        await abandon_large_move(repo, ctx, op_id, batch=batch)
        return
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'queued' "
                "WHERE id = :id AND org_team_id = :org AND state IN ('running', 'failed')"
            ),
            {"id": op_id, "org": repo.scope.org_team_id},
        )
    await run_large_move(repo, ctx, op_id, batch=batch, clock=clock, checkpoints=checkpoints)


async def cursor_matches_rows(repo: FilesRepo, plan: LargeMovePlan) -> bool:
    """Does the parked cursor still name a row of this move's subtree?

    The cursor is the id a committed batch stopped at, so it has to be a node
    in one half of the subtree or the other. A cursor that names nothing there
    is not a position the loop can continue from: the batch statement takes
    rows with ``id > cursor``, so a bogus cursor silently drops everything
    ordered before it.
    """
    if plan.cursor is None:
        return True
    async with repo.transaction():
        found = (
            await repo.session.execute(
                text(
                    "SELECT 1 FROM file_nodes WHERE org_team_id = :org "  # noqa: S608
                    "AND id = CAST(:cursor AS uuid) "
                    f"AND ({subtree_sql('path_ids', 'CAST(:old AS ltree)')} "
                    f"OR {subtree_sql('path_ids', 'CAST(:new AS ltree)')}) LIMIT 1"
                ),
                {
                    "org": repo.scope.org_team_id,
                    "cursor": plan.cursor,
                    **_both_paths(plan),
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).first()
    return found is not None


async def inconsistent_rows(repo: FilesRepo, plan: LargeMovePlan) -> list[uuid.UUID]:
    """The rows of the moved subtree whose path disagrees with their parent's.

    The invariant a batched rewrite can break is the one the adjacency list
    defines: a node's ``path_ids`` is its parent's plus its own label, and its
    ``depth`` is one past its parent's. Checking it against the parent row —
    not against what the loop meant to write — is what makes this a check
    rather than a restatement of the loop.
    """
    async with repo.transaction():
        rows = (
            await repo.session.execute(
                text(
                    "SELECT n.id FROM file_nodes AS n "  # noqa: S608
                    "JOIN file_nodes AS p ON p.id = n.parent_id "
                    "WHERE n.org_team_id = :org AND p.org_team_id = :org "
                    f"AND ({subtree_sql('n.path_ids', 'CAST(:old AS ltree)')} "
                    f"OR {subtree_sql('n.path_ids', 'CAST(:new AS ltree)')}) "
                    "AND (subpath(n.path_ids, 0, nlevel(n.path_ids) - 1) <> p.path_ids "
                    "OR n.depth <> p.depth + 1) ORDER BY n.id"
                ),
                {"org": repo.scope.org_team_id, **_both_paths(plan), **SUBTREE_DEPTH_BIND},
            )
        ).fetchall()
    return [uuid.UUID(str(row[0])) for row in rows]


async def abandon_large_move(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = MOVE_BATCH,
) -> list[uuid.UUID]:
    """Wind up a move that cannot be resumed from its own record.

    The ``moving`` flag is the only thing standing between a half-rewritten
    subtree and a writer, so it is never dropped on the way out of a failure:
    it comes off only once the committed batches have been checked and the tree
    is whole again. Two outcomes, and the check decides which:

    * rows merely *lag* — the loop had not reached them — so the move was
      resumable after all. The rewrite is finished from the start of the id
      order (the parked cursor is exactly what is not trusted here), the flag
      is cleared and the operation completes.
    * rows are *inconsistent* with their parent, which no rewrite of this plan
      can repair. The operation is marked ``failed``, the offending ids are
      named in its ``errors``, and the subtree stays ``moving`` so nothing
      writes into a tree an operator has not looked at yet.

    Returns the inconsistent ids — empty when the move was finished.
    """
    plan = await load_plan(repo, op_id)
    node = await _require(repo, plan.node_id)
    if not plan.root_moved:
        # No batch ever ran, so the old tree IS the whole tree; there is
        # nothing to check and only the mark to take off.
        await _clear_moving(repo, plan)
        await _fail(repo, op_id, "the move was abandoned before its root moved")
        return []

    plan = await _forget_cursor(repo, op_id, plan)
    while await _rewrite_batch(repo, op_id, plan, batch=batch):
        plan = await load_plan(repo, op_id)

    broken = await inconsistent_rows(repo, plan)
    if broken:
        await _fail(
            repo,
            op_id,
            "the moved subtree is inconsistent and stays marked moving; rows: "
            + ", ".join(str(one) for one in broken),
        )
        return broken

    await _finish(repo, ctx, op_id, plan, drive_id=DriveId(node.drive_id))
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'done' "
                "WHERE id = :id AND org_team_id = :org AND state <> 'cancelled'"
            ),
            {"id": op_id, "org": repo.scope.org_team_id},
        )
    return []


def _both_paths(plan: LargeMovePlan) -> dict[str, str | None]:
    """The two halves a crashed move can be split over, as bind parameters.

    ``new`` is NULL before the root has moved, and an ancestry test against a
    NULL path is NULL — never true — so the same predicate covers both stages
    without a second statement or a string-built array literal.
    """
    return {"old": plan.old_path, "new": plan.new_path}


async def _forget_cursor(repo: FilesRepo, op_id: OperationId, plan: LargeMovePlan) -> LargeMovePlan:
    """Drop the parked cursor so the sweep starts at the beginning of the order."""
    body = plan.dump()
    body.pop("cursor", None)
    async with repo.transaction():
        await _store_plan(repo, op_id, body)
    return LargeMovePlan(body)


async def _fail(repo: FilesRepo, op_id: OperationId, reason: str) -> None:
    """Park the reason on the operation row without touching the subtree."""
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'failed', "
                "errors = errors || CAST(:error AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "id": op_id,
                "org": repo.scope.org_team_id,
                "error": json.dumps([reason]),
            },
        )


# ---- the body -------------------------------------------------------------


async def _drive(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    progress: Progress,
    *,
    batch: int,
    checkpoints: Checkpoints,
) -> None:
    plan = await load_plan(repo, op_id)
    node = await _require(repo, plan.node_id)
    drive_id = DriveId(node.drive_id)

    if not plan.root_moved:
        if await progress.cancelled():
            # Nothing has happened to the tree yet, so stopping here IS the old
            # state, and no flag was set, so there is none to clear.
            raise OperationCancelled(str(op_id))
        await _mark_moving(repo, plan)
        await checkpoints.reach("large_move.after_mark")
        try:
            plan = await _move_root(repo, op_id, plan)
        except BaseException:
            await _clear_moving(repo, plan)
            raise

    cancelled = False
    while True:
        moved = await _rewrite_batch(repo, op_id, plan, batch=batch)
        if moved == 0:
            break
        await progress.tick(moved)
        await checkpoints.reach("large_move.after_batch")
        plan = await load_plan(repo, op_id)
        # A cancel after the root moved cannot stop the loop: the rows it has
        # not reached would name a path their parent no longer has. It is
        # recorded and honoured once the tree is whole again.
        cancelled = cancelled or await progress.cancelled()

    await _finish(repo, ctx, op_id, plan, drive_id=drive_id)
    if cancelled:
        raise OperationCancelled(str(op_id))


async def load_plan(repo: FilesRepo, op_id: OperationId) -> LargeMovePlan:
    """Read the plan and cursor off the operation row."""
    async with repo.transaction():
        row = (
            await repo.session.execute(
                text(
                    "SELECT result FROM file_ops "
                    "WHERE id = :id AND org_team_id = :org AND kind = 'move'"
                ),
                {"id": op_id, "org": repo.scope.org_team_id},
            )
        ).first()
    if row is None or not row[0]:
        raise NotFound(f"no large move {op_id}")
    return LargeMovePlan(dict(row[0]))


async def _mark_moving(repo: FilesRepo, plan: LargeMovePlan) -> None:
    """Stamp the whole subtree ``moving``, refusing an overlapping move first.

    Overlap is tested in both directions along the chain: a node under this
    root, and this root under someone else's. A descendant-only test would let
    two moves whose subtrees nest run at once, and the inner one's rewrite
    would be undone by the outer one's.
    """
    async with repo.transaction():
        clash = (
            await repo.session.execute(
                text(
                    "SELECT 1 FROM file_nodes WHERE org_team_id = :org AND state = 'moving' "  # noqa: S608
                    f"AND ({subtree_sql('path_ids', 'CAST(:path AS ltree)')} "
                    f"OR {subtree_sql('CAST(:path AS ltree)', 'path_ids')}) "
                    "LIMIT 1"
                ),
                {"org": repo.scope.org_team_id, "path": plan.old_path, **SUBTREE_DEPTH_BIND},
            )
        ).first()
        if clash is not None:
            raise Conflict("files.moving", "a move of an overlapping subtree is in progress")
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET state = 'moving' "  # noqa: S608
                "WHERE org_team_id = :org "
                f"AND {subtree_sql('path_ids', 'CAST(:path AS ltree)')} "
                "AND state = 'live'"
            ),
            {"org": repo.scope.org_team_id, "path": plan.old_path, **SUBTREE_DEPTH_BIND},
        )


async def _clear_moving(repo: FilesRepo, plan: LargeMovePlan) -> None:
    """Put the subtree back to ``live``, wherever its rows are by now.

    Both paths are cleared because a crash can leave the subtree split across
    them, and clearing only the one the plan reached would strand the rest
    ``moving`` with no operation to finish them.
    """
    paths = [plan.old_path] + ([plan.new_path] if plan.new_path is not None else [])
    async with repo.transaction():
        for path in paths:
            await repo.session.execute(
                text(
                    "UPDATE file_nodes SET state = 'live' "  # noqa: S608
                    "WHERE org_team_id = :org "
                    f"AND {subtree_sql('path_ids', 'CAST(:path AS ltree)')} "
                    "AND state = 'moving'"
                ),
                {"org": repo.scope.org_team_id, "path": path, **SUBTREE_DEPTH_BIND},
            )


async def _move_root(repo: FilesRepo, op_id: OperationId, plan: LargeMovePlan) -> LargeMovePlan:
    """Re-parent the root, with the small move's cycle guard, in one statement.

    The new path lands on the operation row in the same transaction as the
    move, so a crash between the two is not a state the resume can observe.
    """
    node = await _require(repo, plan.node_id)
    async with repo.transaction():
        await repo.lock_chain(
            DriveId(node.drive_id), plan.new_parent_id, plan.node_id, rewrites_subtree=True
        )
        moved = (
            await repo.session.execute(
                text(
                    "UPDATE file_nodes AS n SET parent_id = p.id, "  # noqa: S608
                    "path_ids = p.path_ids || CAST(:label AS ltree), "
                    "depth = nlevel(p.path_ids), etag = n.etag + 1, updated_at = now() "
                    "FROM file_nodes AS p "
                    "WHERE n.id = :id AND n.org_team_id = :org AND n.etag = :if_match "
                    "AND n.trashed_at IS NULL AND p.id = :parent AND p.org_team_id = :org "
                    "AND p.trashed_at IS NULL "
                    f"AND NOT {subtree_sql('p.path_ids', 'n.path_ids')} "
                    "RETURNING n.path_ids::text"
                ),
                {
                    "id": plan.node_id,
                    "org": repo.scope.org_team_id,
                    "if_match": plan.if_match,
                    "parent": plan.new_parent_id,
                    "label": ino_label(node.ino),
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).first()
        if moved is None:
            raise Conflict("files.cycle", "a node may not be moved inside its own subtree")
        body = plan.dump()
        body["new_path"] = str(moved[0])
        await _store_plan(repo, op_id, body)
    return LargeMovePlan(body)


async def _rewrite_batch(
    repo: FilesRepo, op_id: OperationId, plan: LargeMovePlan, *, batch: int
) -> int:
    """Rewrite up to ``batch`` descendants and advance the cursor, in one txn.

    The cursor is an id, not an offset: a rewritten row is no longer under
    ``old_path``, so an offset would skip rows every time the set shrank
    underneath it. Ordering by id is what makes the cursor total.
    """
    if plan.new_path is None:  # pragma: no cover - the caller moves the root first
        raise PreconditionFailed("the root has not moved yet")
    async with repo.transaction():
        rows = (
            await repo.session.execute(
                text(
                    "WITH batch AS ("  # noqa: S608
                    "  SELECT id FROM file_nodes "
                    "  WHERE org_team_id = :org "
                    f"  AND {subtree_sql('path_ids', 'CAST(:old_path AS ltree)')} "
                    "  AND id <> :root AND id > CAST(:cursor AS uuid) "
                    "  ORDER BY id LIMIT :limit"
                    ") "
                    "UPDATE file_nodes SET "
                    "path_ids = CAST(:new_path AS ltree) || subpath(path_ids, :old_len), "
                    "depth = depth + :delta, updated_at = now() "
                    "FROM batch WHERE file_nodes.id = batch.id "
                    "RETURNING file_nodes.id"
                ),
                {
                    "org": repo.scope.org_team_id,
                    "old_path": plan.old_path,
                    "new_path": plan.new_path,
                    "old_len": plan.old_path.count(".") + 1,
                    "delta": plan.depth_delta,
                    "root": plan.node_id,
                    "cursor": plan.cursor if plan.cursor is not None else MIN_UUID,
                    "limit": batch,
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).fetchall()
        if not rows:
            return 0
        body = plan.dump()
        body["cursor"] = str(max(row[0] for row in rows))
        await _store_plan(repo, op_id, body)
    return len(rows)


async def _finish(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    plan: LargeMovePlan,
    *,
    drive_id: DriveId,
) -> None:
    """Clear ``moving`` and announce the move — one history row, one outbox row.

    One row for the root and none for the 20,000 descendants: what changed is
    that one node's parent. A per-node event would drown every consumer in rows
    that say nothing a subtree read does not already imply.
    """
    await _clear_moving(repo, plan)
    node = await _require(repo, plan.node_id)
    async with repo.transaction():
        # The subtree hangs under different ancestors now, so every cached ACL
        # in it describes a chain that no longer exists. Marking is what makes
        # those nodes read through to the chain — the truth — until the rewrite
        # this queues repairs them. Only marking: the one root event below is a
        # deliberate property of this path, and re-deriving here would add a
        # second one. Direct grants are untouched and travel with their nodes.
        await acl.invalidate_subtree(repo, ctx, node)
        await history.record(
            repo,
            ctx,
            node_id=plan.node_id,
            kind="move",
            before={"depth": node.depth - plan.depth_delta},
            after={"depth": node.depth},
            op_id=op_id,
        )
        await history.emit_node_changed(
            repo, ctx, node_id=plan.node_id, drive_id=drive_id, version=node.etag
        )


# ---- internals ------------------------------------------------------------


async def _store_plan(repo: FilesRepo, op_id: OperationId, body: dict[str, Any]) -> None:
    await repo.session.execute(
        text(
            "UPDATE file_ops SET result = CAST(:body AS jsonb) "
            "WHERE id = :id AND org_team_id = :org"
        ),
        {"id": op_id, "org": repo.scope.org_team_id, "body": json.dumps(body)},
    )


async def _require(repo: FilesRepo, node_id: NodeId) -> FileNode:
    async with repo.transaction():
        node = await repo.node(node_id)
        if node is not None:
            await repo.session.refresh(node)
    if node is None:
        raise NotFound(f"no node {node_id}")
    return node


__all__ = [
    "MIN_UUID",
    "MOVE_BATCH",
    "LargeMovePlan",
    "load_plan",
    "plan_body",
    "resume_large_move",
    "run_large_move",
]
