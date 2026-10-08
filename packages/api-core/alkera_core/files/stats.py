"""Folder size, count and mtime: append-only deltas, folded by a job.

This module is the ONE owner of the aggregate. Every byte and count change in
Files appends here, against the changed node's **parent** and never against an
ancestor further up — a leaf write appends one ``file_dir_stats_deltas`` row and
stops, so a thousand writers under one folder contend on nothing: the ancestor's
row keeps its ``xmin`` and no writer waits behind another.

The job then folds each delta **upward** along ``path_ids`` into every
ancestor-or-self, which is what makes ``file_dir_stats`` on any folder — the
drive root included — the total of its whole subtree rather than of its direct
children. Reading a folder's size, and deciding a drive's quota, is therefore one
indexed read and never a SUM over a subtree.

The recount over ``file_nodes``/``file_versions`` is the truth the fold has to
agree with, so a node contributes what a recount would count: a headless file
node contributes 0 bytes and 0 files until a head version lands, and the head
swap that lands it is what appends the bytes and the +1.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, Iterable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import text

from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.models.files.tree import FileNode

#: How many delta rows one fold pass claims. The job loops until a pass folds
#: nothing, so the batch bounds the transaction, not the work.
STATS_FOLD_BATCH = 10_000

#: The default pause points: none. A test passes its own `PausingCheckpoints`.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()


async def add_delta(
    repo: FilesRepo,
    *,
    node_id: NodeId,
    bytes_delta: int,
    files_delta: int,
    direct_children_delta: int,
    child_change_at: datetime | None,
) -> None:
    """Append one increment for the folder ``node_id``.

    ``node_id`` is the PARENT of whatever changed — never the drive root, and
    never any other ancestor: :func:`aggregate` is what carries the number up the
    path, and a caller that charged the root itself would be counted twice once
    the fold reached it. This is an INSERT and never an UPDATE of an ancestor:
    that is the whole point — an UPDATE would make every folder on the path a hot
    row and serialize concurrent writes beneath it behind one another's row lock.
    """
    await repo.add_stats_delta(
        node_id=node_id,
        bytes_delta=bytes_delta,
        files_delta=files_delta,
        direct_children_delta=direct_children_delta,
        child_change_at=child_change_at,
    )


async def add_deltas(
    repo: FilesRepo,
    children: Mapping[uuid.UUID, int],
    *,
    child_change_at: datetime | None,
) -> None:
    """:func:`add_delta` for many folders' direct-child counts, in ONE INSERT.

    ``children`` maps each folder to how many entries it gained (or, negative,
    lost). Only the entry count moves: every caller of this form mints or
    drops headless rows, whose bytes and file counts the head swap and the
    purge account for, never the namespace. A folder whose count nets to zero
    gets no row.
    """
    moved = {folder: count for folder, count in children.items() if count}
    if not moved:
        return
    repo._require_open()
    await repo.session.execute(
        text(
            "INSERT INTO file_dir_stats_deltas (id, org_team_id, node_id, bytes_delta, "
            "files_delta, direct_children_delta, child_change_at) "
            "SELECT t.id, :org, t.node_id, 0, 0, t.children, :at "
            "FROM unnest(CAST(:ids AS uuid[]), CAST(:nodes AS uuid[]), "
            "CAST(:children AS bigint[])) AS t(id, node_id, children)"
        ),
        {
            "org": repo.scope.org_team_id,
            "at": child_change_at,
            "ids": [uuid.uuid4() for _ in moved],
            "nodes": list(moved),
            "children": list(moved.values()),
        },
    )


async def aggregate(
    repo_admin_iter: AsyncIterable[FilesRepo] | Iterable[FilesRepo],
    *,
    batch: int = STATS_FOLD_BATCH,
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> int:
    """Fold every pending delta into ``file_dir_stats``; return rows folded.

    Each repo in ``repo_admin_iter`` is one tenant's scope, folded in its own
    transaction: the ``INSERT … ON CONFLICT DO UPDATE`` and the DELETE of the
    exact delta ids that fed it are the same transaction, so a crash between
    them rolls both back and the next run folds those deltas — exactly once,
    never twice, and the pass is resumable from wherever it died.

    Each delta lands on every ancestor-or-self of the folder it names, so the
    drive root ends up holding the whole drive's total.
    """
    if batch < 1:
        raise ValueError(f"batch must be >= 1, got {batch}")
    folded = 0
    repos = _aiter(repo_admin_iter)
    async for repo in repos:
        while True:
            count = await _fold_once(repo, batch=batch, checkpoints=checkpoints)
            folded += count
            if count < batch:
                break
    return folded


async def _aiter(
    source: AsyncIterable[FilesRepo] | Iterable[FilesRepo],
) -> AsyncIterable[FilesRepo]:
    if isinstance(source, AsyncIterable):
        async for item in source:
            yield item
        return
    for item in source:
        yield item


async def _fold_once(repo: FilesRepo, *, batch: int, checkpoints: Checkpoints) -> int:
    """One transaction: claim up to ``batch`` deltas, fold them, delete them."""
    async with repo.transaction():
        claimed = await repo.claim_stats_deltas(limit=batch)
        if not claimed:
            return 0
        ids = list(claimed)
        await _fold_upward(repo, ids)
        await checkpoints.reach("stats.after_fold_before_delete")
        await repo.drop_stats_deltas(ids)
        return len(ids)


async def _fold_upward(repo: FilesRepo, ids: Sequence[uuid.UUID]) -> None:
    """Add the named deltas into every ancestor-or-self of the folder they name.

    One statement rather than a walk per delta: ancestor-or-self, spelled the
    way :meth:`FilesRepo.subtree_predicate` spells it — the truncated
    prefix is the expression the GiST index is built on, and the exact test
    behind it keeps an ancestor deeper than the truncation correct — so a batch
    of ten thousand deltas costs one index-served join and one grouped upsert.

    ``direct_children`` is the exception that stays local — it counts the
    folder's OWN entries, so it is added only where the ancestor IS the folder
    the delta named, while bytes and files travel all the way to the root.
    """
    if not ids:
        return
    await repo.session.execute(
        text(
            "INSERT INTO file_dir_stats ("  # noqa: S608
            "  node_id, org_team_id, bytes, files, direct_children, last_child_change_at) "
            "SELECT a.id, :org, "
            "  COALESCE(SUM(d.bytes_delta), 0), "
            "  COALESCE(SUM(d.files_delta), 0), "
            "  COALESCE(SUM(CASE WHEN a.id = d.node_id THEN d.direct_children_delta END), 0), "
            "  MAX(d.child_change_at) "
            "FROM file_dir_stats_deltas AS d "
            "JOIN file_nodes AS n ON n.id = d.node_id AND n.org_team_id = d.org_team_id "
            "JOIN file_nodes AS a "
            f"  ON {subtree_sql('n.path_ids', 'a.path_ids')} "
            "  AND a.org_team_id = d.org_team_id "
            "WHERE d.id = ANY(CAST(:ids AS uuid[])) AND d.org_team_id = :org "
            "GROUP BY a.id "
            "ON CONFLICT (node_id) DO UPDATE SET "
            "  bytes = file_dir_stats.bytes + EXCLUDED.bytes, "
            "  files = file_dir_stats.files + EXCLUDED.files, "
            "  direct_children = file_dir_stats.direct_children + EXCLUDED.direct_children, "
            "  last_child_change_at = GREATEST("
            "    file_dir_stats.last_child_change_at, EXCLUDED.last_child_change_at), "
            "  updated_at = now()"
        ),
        {
            "org": repo.scope.org_team_id,
            "ids": [str(one) for one in ids],
            **SUBTREE_DEPTH_BIND,
        },
    )


async def folder_mtime(repo: FilesRepo, node: FileNode) -> int | None:
    """The folder's modification time in nanoseconds, or ``None`` if unknown.

    An explicitly set ``mtime_ns`` wins — a client that stamped a folder means
    it, and a later child change must not overwrite the stamp. Otherwise the
    time is derived from the aggregated ``last_child_change_at``, so a folder
    whose children changed reads as modified without any ancestor ever having
    been written.
    """
    repo._require_open()
    if node.mtime_ns:
        return node.mtime_ns
    changed = await repo.folder_last_child_change(node)
    if changed is None:
        return None
    return int(changed.timestamp() * 1_000_000_000)


def folder_ctag(drive_change_seq: int) -> str:
    """A folder's collection tag for the WebDAV / sync clients.

    Derived from the drive's change sequence rather than from the folder's own
    columns, because a folder row is deliberately not written when a child
    changes: a client polling the ctag must still see it move, and the drive
    sequence is the one number that advances on every change in the drive.
    """
    if drive_change_seq < 0:
        raise ValueError(f"a change sequence is never negative, got {drive_change_seq}")
    return f'"{drive_change_seq:x}"'


__all__ = [
    "STATS_FOLD_BATCH",
    "add_delta",
    "add_deltas",
    "aggregate",
    "folder_ctag",
    "folder_mtime",
]
