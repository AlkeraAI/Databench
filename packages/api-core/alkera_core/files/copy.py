"""A server-side copy of a subtree: new nodes, the same bytes.

Three things make a 40,000-node copy safe to run outside a request.

*No byte moves.* A copied file's head version is a new ``file_versions`` row
that names the SAME ``content_hash``, ``store_key`` and ``inline_bytes`` as the
one it was copied from. The store is never called, so the cost of copying a
2 TB checkpoint is one row, and the quota settles the logical bytes the copy
adds — what an org is billed for is what its tree says it holds, not what the
dedup domain physically stores.

*Every batch is a commit, and the cursor is on the operation row.* Descendants
are created in ``(depth, id)`` order — depth first so a node's parent copy
always exists before the node, id second so the order is total — and the pair
the batch stopped at is written into ``file_ops.result`` in that same
transaction. A crash therefore resumes at the boundary rather than restarting,
and the batches already committed are exactly what an uninterrupted copy would
have left.

*A cancel stops at the next boundary and leaves what it made.* Unlike a move,
a copy is purely additive: every committed batch is a whole tree whose
``path_ids`` agree with the parent walk, so there is nothing to drain. The
partial copy stays, the operation reports ``cancelled``, and re-running the
plan is not offered — the caller decides whether to delete it or copy again.

A copy carries the source's names byte-identically and its POSIX attributes,
and it is renamed on collision at the destination for the ROOT only: a
descendant's name is unique among its own new siblings by construction, because
its siblings are exactly the copies of the source's siblings. ACLs are not
copied: the new subtree inherits the destination's, which is the only reading
under which "copy this into a folder I share with the team" does not silently
publish the source's grants.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.files import history, stats
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock, SystemClock
from alkera_core.files.content import _RESTORE_CARRIED_METADATA
from alkera_core.files.drives import HOME_SUBTYPE, assert_traversal_rules
from alkera_core.files.errors import InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import MIN_UUID, DriveId, NodeId, OperationId
from alkera_core.files.ino import reserve_apart
from alkera_core.files.namespace import Namespace, NodeAttrs, refuse_links_a_move_would_open
from alkera_core.files.ops import OperationCancelled, Operations, Progress
from alkera_core.files.path_labels import ino_label
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.tree import FileNode

#: How many nodes one batch creates. Sized like the move's: large enough that a
#: 20,000-node copy is a handful of statements, small enough that no batch holds
#: its locks past the watchdog's deadline.
COPY_BATCH: Final = 10_000

#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()

#: Where a copied node records the source node it was made from. The batch
#: statement joins a child's source parent to that parent's copy through it, so
#: the mapping never has to be carried on the operation row — which is what
#: keeps a 40,000-node plan the same size as a one-node plan.
COPIED_FROM: Final = "copied_from"

#: The ``version_metadata`` keys a copied version carries off the version it was
#: made from. Exactly the set a restore carries, taken from ``content`` rather
#: than spelled again here so the two can never drift: both rows point at bytes
#: somebody else wrote, and both are unreadable without the digests describing
#: them.
CARRIED_VERSION_METADATA: Final[tuple[str, ...]] = _RESTORE_CARRIED_METADATA


class CopyPlan:
    """What the routing wrote onto the operation row for the runner to pick up."""

    __slots__ = (
        "cursor_depth",
        "cursor_id",
        "dest_parent_id",
        "name",
        "new_root_id",
        "new_root_path",
        "node_id",
        "root_flags",
        "src_path",
    )

    def __init__(self, body: dict[str, Any]) -> None:
        self.node_id = NodeId(uuid.UUID(str(body["node_id"])))
        self.dest_parent_id = NodeId(uuid.UUID(str(body["dest_parent_id"])))
        self.src_path: str = str(body["src_path"])
        raw_name = body.get("name")
        #: The root's new name, when the copy is not called what its source is
        #: (a duplicated chat is "<title> (copy)"). Carried as UTF-8 text with
        #: surrogate escapes so an arbitrary byte name survives the JSON column.
        self.name: bytes | None = (
            None if raw_name is None else str(raw_name).encode("utf-8", "surrogateescape")
        )
        #: Restriction bits the root is stamped with on top of what it inherits
        #: from its new parent — a duplicated chat keeps its seal.
        self.root_flags: int = int(body.get("root_flags") or 0)
        raw_root = body.get("new_root_id")
        self.new_root_id: NodeId | None = (
            None if raw_root is None else NodeId(uuid.UUID(str(raw_root)))
        )
        raw_path = body.get("new_root_path")
        self.new_root_path: str | None = None if raw_path is None else str(raw_path)
        raw_depth = body.get("cursor_depth")
        self.cursor_depth: int | None = None if raw_depth is None else int(raw_depth)
        raw_id = body.get("cursor_id")
        self.cursor_id: str | None = None if raw_id is None else str(raw_id)

    @property
    def root_copied(self) -> bool:
        """Has the root's own copy been made yet?

        The new root's id is what every descendant's parent chain hangs off, so
        its presence — not a separate boolean — is the truthful record of how
        far this got.
        """
        return self.new_root_id is not None

    def dump(self) -> dict[str, Any]:
        """The row body this plan came from, ready to be written back."""
        body = copy_plan_body(
            self.node_id,
            self.dest_parent_id,
            src_path=self.src_path,
            name=self.name,
            root_flags=self.root_flags,
        )
        if self.new_root_id is not None:
            body["new_root_id"] = str(self.new_root_id)
        if self.new_root_path is not None:
            body["new_root_path"] = self.new_root_path
        if self.cursor_depth is not None:
            body["cursor_depth"] = self.cursor_depth
        if self.cursor_id is not None:
            body["cursor_id"] = self.cursor_id
        return body


def copy_plan_body(
    node_id: NodeId,
    dest_parent_id: NodeId,
    *,
    src_path: str,
    name: bytes | None = None,
    root_flags: int = 0,
) -> dict[str, Any]:
    """The plan a routed copy stores on its operation row."""
    body: dict[str, Any] = {
        "node_id": str(node_id),
        "dest_parent_id": str(dest_parent_id),
        "src_path": src_path,
    }
    if name is not None:
        body["name"] = name.decode("utf-8", "surrogateescape")
    if root_flags:
        body["root_flags"] = root_flags
    return body


async def run_copy(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = COPY_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> None:
    """Run — or resume — the batched copy recorded on ``op_id``.

    Resumable by construction: the body reads its plan from the row every time
    round, so a second call after a crash picks up at the cursor rather than at
    the beginning. ``Operations.run`` is what refuses to start the body twice
    concurrently, because its ``queued → running`` claim is a compare-and-swap.
    """
    ops = Operations(repo, ctx, clock if clock is not None else SystemClock())

    async def body(progress: Progress) -> None:
        await _drive(repo, ctx, op_id, progress, batch=batch, checkpoints=checkpoints)

    await ops.run(op_id, body)


async def resume_copy(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    *,
    batch: int = COPY_BATCH,
    clock: Clock | None = None,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> None:
    """Continue a copy whose runner died, from the cursor it left behind.

    The watchdog moves a heartbeat-less operation to ``failed``; a crashed
    runner leaves it ``running`` with a stale heartbeat. Either way the resume
    puts the row back to ``queued`` so the ordinary claim applies — that is
    what keeps one runner per operation whether it is the first or the fifth.
    """
    state = await Operations(repo, ctx, clock if clock is not None else SystemClock()).get(op_id)
    if state.state in ("done", "cancelled"):
        raise PreconditionFailed(f"operation {op_id} is {state.state}; there is nothing to resume")
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'queued' "
                "WHERE id = :id AND org_team_id = :org AND state IN ('running', 'failed')"
            ),
            {"id": op_id, "org": repo.scope.org_team_id},
        )
    await run_copy(repo, ctx, op_id, batch=batch, clock=clock, checkpoints=checkpoints)


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
    plan = await load_copy_plan(repo, op_id)
    source = await _require(repo, plan.node_id)
    drive_id = DriveId(source.drive_id)

    if not plan.root_copied:
        if await progress.cancelled():
            # Nothing exists yet, so stopping here IS the old state.
            raise OperationCancelled(str(op_id))
        plan = await _copy_root(repo, ctx, op_id, plan)
        await checkpoints.reach("copy.after_root")
        await progress.tick(1)

    while True:
        made = await _copy_batch(repo, op_id, plan, batch=batch, checkpoints=checkpoints)
        if made == 0:
            break
        await progress.tick(made)
        await checkpoints.reach("copy.after_batch")
        plan = await load_copy_plan(repo, op_id)
        # A copy is additive, so every committed batch is already a whole tree:
        # a cancel can be honoured at the boundary with nothing to drain.
        if await progress.cancelled():
            raise OperationCancelled(str(op_id))

    await _finish(repo, ctx, op_id, plan, drive_id=drive_id)
    # The copy's bytes are now counted (its deltas are in the quota read the
    # moment they are appended), so this is the write that can take the drive
    # past its ceiling.
    async with repo.transaction():
        await QuotaService(repo, ctx, SystemClock()).freeze_if_over(drive_id)


async def load_copy_plan(repo: FilesRepo, op_id: OperationId) -> CopyPlan:
    """Read the plan and cursor off the operation row."""
    async with repo.transaction():
        row = (
            await repo.session.execute(
                text(
                    "SELECT result FROM file_ops "
                    "WHERE id = :id AND org_team_id = :org AND kind = 'copy'"
                ),
                {"id": op_id, "org": repo.scope.org_team_id},
            )
        ).first()
    if row is None or not row[0]:
        raise NotFound(f"no copy {op_id}")
    return CopyPlan(dict(row[0]))


async def _copy_root(
    repo: FilesRepo, ctx: ActingContext, op_id: OperationId, plan: CopyPlan
) -> CopyPlan:
    """Create the root's copy under the destination, renaming on collision.

    The new id lands on the operation row in the same transaction as the node,
    so a crash between the two is not a state the resume can observe.
    """
    async with repo.transaction():
        source = await repo.node(plan.node_id)
        if source is None:
            raise NotFound(f"no node {plan.node_id}")
        namespace = Namespace(repo, ctx, SystemClock())
        made = await namespace.create(
            DriveId(source.drive_id),
            plan.dest_parent_id,
            source.kind,
            plan.name if plan.name is not None else bytes(source.name),
            attrs=_attrs_of(source),
            conflict="rename",
            symlink_target=source.symlink_target,
            symlink_kind=source.symlink_kind,
            # A copy of a member's home is an ordinary folder: the home mark
            # is the one identity a copy must not carry.
            subtype=None if source.subtype == HOME_SUBTYPE else source.subtype,
        )
        await _stamp_source(repo, NodeId(made.id), plan.node_id, flags=plan.root_flags)
        await _copy_head_version(repo, source_ids=[plan.node_id], new_root_path=str(made.path_ids))
        if source.kind == "file" and source.head_version_id is not None:
            # The namespace counted the new entry and no bytes — the head swap
            # is what lands them, and for a root that swap is the line above.
            # Descendants are charged by their batch; the root is charged here,
            # or a one-file copy would cost its copier nothing.
            await stats.add_delta(
                repo,
                node_id=plan.dest_parent_id,
                bytes_delta=source.size,
                files_delta=1,
                direct_children_delta=0,
                child_change_at=None,
            )
        body = plan.dump()
        body["new_root_id"] = str(made.id)
        body["new_root_path"] = str(made.path_ids)
        await _store_plan(repo, op_id, body)
    return CopyPlan(body)


async def _copy_batch(
    repo: FilesRepo,
    op_id: OperationId,
    plan: CopyPlan,
    *,
    batch: int,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> int:
    """Create up to ``batch`` descendant copies and advance the cursor.

    The cursor is ``(depth, id)`` rather than an id alone because the order has
    to put a parent before its children — a copy cannot hang off a parent that
    does not exist yet — and depth alone is not total. Ordering by the pair and
    resuming from it is what makes the batching both correct and resumable.

    Two transactions, not one. The window is read and its inode numbers are
    claimed in the first, which commits at once; the nodes, their versions and
    their stats land in the second. ``file_drives`` is the row every writer in
    the org advances, and the claim is an UPDATE on it: claimed inside the
    batch's own transaction it kept the row locked for the whole batch, and
    every other write of the org — and, through the drive resolve, every read —
    queued behind it for that long. A claim that commits and is then not used
    (a crash between the two) wastes a few numbers, which the counter's design
    already allows; nothing reads them as a count.
    """
    if plan.new_root_path is None:  # pragma: no cover - the caller copies the root first
        raise PreconditionFailed("the root has not been copied yet")
    async with repo.transaction():
        window = (
            await repo.session.execute(
                text(_WINDOW_SQL),
                {
                    "org": repo.scope.org_team_id,
                    "src_path": plan.src_path,
                    "src_root": plan.node_id,
                    "cursor_depth": plan.cursor_depth if plan.cursor_depth is not None else -1,
                    "cursor_id": plan.cursor_id if plan.cursor_id is not None else MIN_UUID,
                    "limit": batch,
                    **SUBTREE_DEPTH_BIND,
                },
            )
        ).fetchall()
        if not window:
            return 0
        # One depth level per batch: the insert places every node under the copy
        # of its own parent, and a row inserted by the same statement is not
        # visible to that statement's join — so a batch that spanned two levels
        # would drop the deeper one on the floor.
        level = int(window[0][1])
        window = [row for row in window if int(row[1]) == level]
        sources = [uuid.UUID(str(row[0])) for row in window]
        first_ino = await _claim_inos(repo, DriveId(window[0][2]), count=len(sources))
    inos = list(range(first_ino, first_ino + len(sources)))
    async with repo.transaction():
        await checkpoints.reach("copy.in_batch")
        made_ids = (
            (
                await repo.session.execute(
                    text(_INSERT_SQL),
                    {
                        "org": repo.scope.org_team_id,
                        "new_root_path": plan.new_root_path,
                        **SUBTREE_DEPTH_BIND,
                        "sources": [str(one) for one in sources],
                        "inos": inos,
                        "labels": [ino_label(one) for one in inos],
                    },
                )
            )
            .scalars()
            .all()
        )
        if len(made_ids) != len(sources):
            # A source whose parent's copy is missing cannot be placed, and
            # continuing past it would report success over a tree with a hole.
            raise PreconditionFailed(
                f"copy {op_id} placed {len(made_ids)} of {len(sources)} nodes; "
                "a parent's copy is missing"
            )
        await _copy_head_version(
            repo, source_ids=[NodeId(one) for one in sources], new_root_path=plan.new_root_path
        )
        await _add_stats(repo, [uuid.UUID(str(one)) for one in made_ids])
        last = max((int(row[1]), uuid.UUID(str(row[0]))) for row in window)
        body = plan.dump()
        body["cursor_depth"] = last[0]
        body["cursor_id"] = str(last[1])
        await _store_plan(repo, op_id, body)
    return len(sources)


async def _claim_inos(repo: FilesRepo, drive_id: DriveId, *, count: int) -> int:
    """Take ``count`` consecutive inos off the drive's counter; return the first.

    One statement, so two batches racing on the same drive cannot be handed the
    same ino: the counter is advanced and read in the same update. It commits
    apart (:func:`~alkera_core.files.ino.reserve_apart`): advanced inside the
    batch, it held the org's drive row to the batch's commit, queueing every
    other writer in the org behind the copy.
    """
    try:
        block = await reserve_apart(repo, drive_id, block=count)
    except LookupError as exc:  # pragma: no cover - the drive is read before the copy starts
        raise NotFound(f"no drive {drive_id}") from exc
    return block.next


#: The next ``(depth, id)`` window of the source subtree, cursor-bounded. The
#: subtree is spelled the one way Files spells it, so it is served by the same
#: index every other subtree scan is planned against.
_WINDOW_SQL: Final = f"""
SELECT n.id, n.depth, n.drive_id
FROM file_nodes AS n
WHERE n.org_team_id = :org
  AND {subtree_sql("n.path_ids", "CAST(:src_path AS ltree)")}
  AND n.id <> :src_root AND n.trashed_at IS NULL
  AND (n.depth, n.id) > (:cursor_depth, CAST(:cursor_id AS uuid))
ORDER BY n.depth, n.id LIMIT :limit
"""  # noqa: S608

#: One batch's nodes, each inserted under the copy of its own parent — found
#: through ``COPIED_FROM``, which is why the old→new mapping never has to be
#: carried on the operation row.
_INSERT_SQL: Final = f"""
INSERT INTO file_nodes (
  id, ino, drive_id, org_team_id, parent_id, kind, subtype, name, name_display, name_key,
  name_encoding, flags_names, path_ids, depth, symlink_target, symlink_kind, mode, uid, gid,
  nlink, rdev,
  size, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, state, trust,
  traversal_only, metadata, created_by)
SELECT
  gen_random_uuid(), t.ino,
  b.drive_id, :org, p.id, b.kind, b.subtype, b.name, b.name_display, b.name_key,
  b.name_encoding, b.flags_names,
  p.path_ids || CAST(t.label AS ltree),
  p.depth + 1, b.symlink_target, b.symlink_kind, b.mode, b.uid, b.gid, 1, b.rdev,
  b.size, b.atime_ns, b.mtime_ns, (EXTRACT(EPOCH FROM now()) * 1000000000)::bigint,
  b.birthtime_ns, b.xattrs, 1, b.flags, 'live', b.trust, b.traversal_only,
  jsonb_build_object('copied_from', b.id::text), b.created_by
FROM unnest(CAST(:sources AS uuid[]), CAST(:inos AS bigint[]), CAST(:labels AS text[]))
     AS t(source_id, ino, label)
JOIN file_nodes AS b ON b.id = t.source_id AND b.org_team_id = :org
JOIN file_nodes AS p ON p.org_team_id = :org
  AND {subtree_sql("p.path_ids", "CAST(:new_root_path AS ltree)")}
  AND p.metadata ->> 'copied_from' = b.parent_id::text
RETURNING file_nodes.id
"""  # noqa: S608


async def _copy_head_version(
    repo: FilesRepo, *, source_ids: list[NodeId], new_root_path: str
) -> None:
    """Point each new file's head at a version naming the source's own bytes.

    A version row belongs to a node, so the copy gets its own row — carrying the
    source's ``content_hash``, ``store_key`` and ``inline_bytes`` unchanged, so
    the store is never called and the GC's reachability pass sees two versions
    naming one object. Its ``source`` is ``copy``, not the source version's own:
    the bytes are shared, but how *this* version came to exist is what the
    history pane shows and what the retention policy reasons about, and a copy
    that claims to be the upload it was made from tells both of them a lie.

    The ``version_metadata`` keys that describe the BYTES come across for the
    same reason a restore carries them: ``open`` checks every block against the
    digest its version records and refuses a version that has none, so a copy
    naming the source's object without the source's ``block_hashes`` would be a
    file nobody could read. ``dedup_domain_id`` comes with them because it names
    the domain prefix the shared object lives under. Nothing else does — the
    keys about the write that produced the bytes belong to that write.
    """
    if not source_ids:
        return
    await repo.session.execute(
        text(
            "WITH pairs AS ("  # noqa: S608
            "  SELECT c.id AS new_id, v.* FROM file_nodes AS c "
            "  JOIN file_nodes AS s ON s.id = CAST(c.metadata ->> 'copied_from' AS uuid) "
            "    AND s.org_team_id = :org "
            "  JOIN file_versions AS v ON v.id = s.head_version_id AND v.org_team_id = :org "
            "  WHERE c.org_team_id = :org "
            f"    AND {subtree_sql('c.path_ids', 'CAST(:new_root_path AS ltree)')} "
            "    AND c.head_version_id IS NULL "
            "    AND CAST(c.metadata ->> 'copied_from' AS uuid) = ANY(CAST(:sources AS uuid[]))"
            "), made AS ("
            "  INSERT INTO file_versions ("
            "    id, org_team_id, node_id, seq, size_bytes, content_hash, block_hash, "
            "    manifest_id, inline_bytes, store_key, mime_sniffed, scan_state, source, "
            "    keep_forever, held, lease_epoch, metadata, created_by) "
            "  SELECT gen_random_uuid(), :org, pairs.new_id, 1, pairs.size_bytes, "
            "    pairs.content_hash, pairs.block_hash, pairs.manifest_id, pairs.inline_bytes, "
            "    pairs.store_key, pairs.mime_sniffed, pairs.scan_state, 'copy', "
            "    false, false, 0, COALESCE(("
            "      SELECT jsonb_object_agg(carried.key, pairs.metadata -> carried.key) "
            "      FROM unnest(CAST(:carried AS text[])) AS carried(key) "
            "      WHERE pairs.metadata -> carried.key IS NOT NULL"
            "    ), '{}'::jsonb), pairs.created_by "
            "  FROM pairs RETURNING id, node_id"
            ") "
            "UPDATE file_nodes SET head_version_id = made.id "
            "FROM made WHERE file_nodes.id = made.node_id AND file_nodes.org_team_id = :org"
        ),
        {
            "org": repo.scope.org_team_id,
            "sources": [str(one) for one in source_ids],
            "new_root_path": new_root_path,
            "carried": list(CARRIED_VERSION_METADATA),
            **SUBTREE_DEPTH_BIND,
        },
    )


async def _add_stats(repo: FilesRepo, made_ids: list[uuid.UUID]) -> None:
    """One aggregated delta per new parent, so the quota settles logical bytes.

    Grouped rather than one row per node: the aggregator folds either shape to
    the same numbers, and 20,000 single-row inserts would cost more than the
    copy itself.

    Bytes and files count only what a recount of ``file_versions`` counts — a
    file carrying a head version. ``direct_children`` counts every made node,
    head or not, because that is the folder's own entry count.
    """
    await repo.session.execute(
        text(
            "INSERT INTO file_dir_stats_deltas ("
            "  id, org_team_id, node_id, bytes_delta, files_delta, direct_children_delta, "
            "  child_change_at) "
            "SELECT gen_random_uuid(), :org, n.parent_id, "
            "  COALESCE(SUM(n.size) FILTER ("
            "    WHERE n.kind = 'file' AND n.head_version_id IS NOT NULL), 0), "
            "  COUNT(*) FILTER (WHERE n.kind = 'file' AND n.head_version_id IS NOT NULL), "
            "  COUNT(*), now() "
            "FROM file_nodes AS n "
            "WHERE n.org_team_id = :org AND n.id = ANY(CAST(:ids AS uuid[])) "
            "GROUP BY n.parent_id"
        ),
        {"org": repo.scope.org_team_id, "ids": [str(one) for one in made_ids]},
    )


async def _stamp_source(
    repo: FilesRepo, new_id: NodeId, source_id: NodeId, *, flags: int = 0
) -> None:
    """Record which source node a copy was made from, on the copy — and, for a
    root the plan seals, the restriction bits it must wear from birth.

    Descendants carry their source's own bits through the batch insert; the
    root is the one node the namespace created from its NEW parent, so what its
    source wore has to be put back here or a duplicated chat is born unsealed.
    """
    await repo.session.execute(
        text(
            "UPDATE file_nodes SET metadata = metadata || CAST(:mark AS jsonb), "
            "flags = flags | :flags WHERE id = :id AND org_team_id = :org"
        ),
        {
            "id": new_id,
            "org": repo.scope.org_team_id,
            "mark": json.dumps({COPIED_FROM: str(source_id)}),
            "flags": flags,
        },
    )


async def _finish(
    repo: FilesRepo,
    ctx: ActingContext,
    op_id: OperationId,
    plan: CopyPlan,
    *,
    drive_id: DriveId,
) -> None:
    """Announce the copy — one history row, one outbox row, for the new root.

    One row for the root and none for the 20,000 descendants: what changed is
    that one folder gained one child. A per-node event would drown every
    consumer in rows that a subtree read already implies.

    The history kind is ``create``: a copy IS the creation of the new root, and
    the source it came from is on the row's ``after`` snapshot rather than in a
    kind of its own.
    """
    if plan.new_root_id is None:  # pragma: no cover - the loop copies the root first
        raise PreconditionFailed("the root has not been copied yet")
    new_root = await _require(repo, plan.new_root_id)
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET result_node_id = :node WHERE id = :id AND org_team_id = :org"
            ),
            {"id": op_id, "org": repo.scope.org_team_id, "node": plan.new_root_id},
        )
        await history.record(
            repo,
            ctx,
            node_id=plan.new_root_id,
            kind="copy",
            before=None,
            after={
                "parent_id": str(plan.dest_parent_id),
                "copied_from": str(plan.node_id),
                "ino": new_root.ino,
            },
            op_id=op_id,
        )
        await history.emit_node_changed(
            repo, ctx, node_id=plan.new_root_id, drive_id=drive_id, version=new_root.etag
        )


# ---- internals ------------------------------------------------------------


def _attrs_of(node: FileNode) -> NodeAttrs:
    """The source's stat block, carried onto its copy.

    The ctime is deliberately not among them: it is when THIS row was created,
    which is now, and a copy that claimed the source's would misreport its own
    age to every client that stats it.
    """
    return NodeAttrs(
        mode=node.mode,
        uid=node.uid,
        gid=node.gid,
        size=node.size,
        atime_ns=node.atime_ns,
        mtime_ns=node.mtime_ns,
        birthtime_ns=node.birthtime_ns,
        xattrs=dict(node.xattrs or {}),
    )


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


async def start_copy(
    repo: FilesRepo,
    ops: Operations,
    *,
    node: FileNode,
    dest_parent: FileNode,
    name: bytes | None = None,
    root_flags: int = 0,
) -> OperationId:
    """Queue a copy of ``node`` into ``dest_parent`` and return its id.

    The plan is written in the same transaction as the operation row, so a row
    a worker picks up always carries a plan; the route calls this rather than
    ``Operations.start`` so the two can never drift apart.

    ``name`` renames the root (collisions still resolve by the rename rule);
    ``root_flags`` are restriction bits the root wears on top of what its new
    parent hands down — the seal a duplicated chat keeps.
    """
    if node.kind not in ("folder", "file", "symlink"):
        raise InvalidRequest(f"a {node.kind} cannot be copied")
    # The destination is refused here, not by the runner: a copy queued into a
    # signpost would only defer the refusal to a worker nobody is watching, and
    # answer the caller 202 for a copy that can never land.
    assert_traversal_rules(dest_parent)
    # A link copied shallower than its source can come to point out of the
    # drive; refused now, the way a move is, never discovered by the runner.
    refuse_links_a_move_would_open(
        await repo.nodes_by_path_prefix(node.path_ids),
        root_depth=node.depth,
        new_root_depth=dest_parent.depth + 1,
    )
    state = await ops.start("copy", drive_id=DriveId(node.drive_id))
    await _store_plan(
        repo,
        state.id,
        copy_plan_body(
            NodeId(node.id),
            NodeId(dest_parent.id),
            src_path=str(node.path_ids),
            name=name,
            root_flags=root_flags,
        ),
    )
    return state.id


__all__ = [
    "COPIED_FROM",
    "COPY_BATCH",
    "MIN_UUID",
    "CopyPlan",
    "copy_plan_body",
    "load_copy_plan",
    "resume_copy",
    "run_copy",
    "start_copy",
]
