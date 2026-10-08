"""`create_tree`: the folder skeleton of a dropped directory, in one round trip.

A browser that drops a 5,000-file directory must not make 5,000 requests to
create the folders those files land in. So the whole skeleton is one call, and
one call is one statement *per depth level* — the levels are sequential because
a child's `path_ids` is built from its parent's, and the parent's `ino` is only
known once the parent row exists.

Idempotency here is structural rather than a stored response: the insert is
``ON CONFLICT (parent_id, name) WHERE trashed_at IS NULL DO NOTHING``, and each
level's statement returns the ids of the rows it inserted **and** of the rows
that were already there. A replay therefore returns the same ids and creates
nothing, whether the first attempt committed, crashed halfway, or is being
retried by a client that never saw the response. `idempotency_key` rides along
so the history rows of one skeleton share a correlation id.

Refusal is total: a path with a segment the naming contract rejects fails the
whole call before any statement runs, so a caller never has to reason about a
half-made skeleton.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import bindparam, text

from alkera_core.authz.principal import ActingContext
from alkera_core.files import history, names
from alkera_core.files.authz.decider import flags_for_child
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound
from alkera_core.files.history import owner_ref
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.leases import LeaseContext, fenced_write_for
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo

#: How deep a dropped skeleton may be. Each level costs one statement, and the
#: `path_ids` ltree is what a deeper tree would eventually outgrow.
MAX_TREE_DEPTH: Final = 64

#: How many distinct folders one call may create. The `INSERT` for a level is a
#: single multi-row statement, so the bound is on the whole skeleton, not a page.
#:
#: A guard against a statement no server should be asked to plan, NOT a policy
#: about how large a directory anyone may drop: a drop is refused WHOLE, so a
#: number anywhere near a real working tree turns a routine drag of a
#: node_modules-sized directory into a wholesale failure with nothing to retry.
#: This sits far above any of them. The client sends one call per drop and never
#: splits it, which is what makes the total — not a page — the thing bounded.
MAX_TREE_FOLDERS: Final = 1_000_000

_NO_CHECKPOINTS: Final = NoopCheckpoints()

#: One level: insert what is missing, then read back both what was inserted and
#: what already existed, as one statement. `ON CONFLICT DO NOTHING` returns no
#: row for a conflict, which is why the `UNION ALL` half is here — without it a
#: replay would come back with no ids at all.
_LEVEL_SQL: Final = """
WITH wanted AS (
    SELECT * FROM unnest(
        CAST(:ids AS uuid[]), CAST(:inos AS bigint[]), CAST(:parents AS uuid[]),
        CAST(:names AS bytea[]), CAST(:displays AS text[]), CAST(:keys AS text[]),
        CAST(:paths AS text[]), CAST(:name_flags AS jsonb[]), CAST(:flags AS integer[])
    ) AS t(id, ino, parent_id, name, name_display, name_key, path_ids, flags_names, flags)
    CROSS JOIN LATERAL (
        SELECT (EXTRACT(EPOCH FROM now()) * 1000000000)::bigint AS now_ns
    ) AS stamp
),
inserted AS (
    INSERT INTO file_nodes (
        id, ino, drive_id, org_team_id, parent_id, kind,
        name, name_display, name_key, flags_names, path_ids, depth, created_by,
        mode, uid, gid, nlink, size, rdev,
        atime_ns, mtime_ns, ctime_ns, birthtime_ns,
        xattrs, etag, flags, traversal_only, metadata
    )
    SELECT w.id, w.ino, :drive_id, :org_team_id, w.parent_id, 'folder',
           w.name, w.name_display, w.name_key, w.flags_names,
           CAST(w.path_ids AS ltree), :depth, :created_by,
           493, 0, 0, 1, 0, 0,
           w.now_ns, w.now_ns, w.now_ns, w.now_ns,
           '{}'::jsonb, 1, w.flags, FALSE, '{}'::jsonb
    FROM wanted w
    ON CONFLICT (parent_id, name) WHERE trashed_at IS NULL DO NOTHING
    RETURNING id, ino, parent_id, name, path_ids, flags
)
SELECT id, ino, parent_id, name, CAST(path_ids AS text) AS path_ids, flags, TRUE AS created
FROM inserted
UNION ALL
SELECT n.id, n.ino, n.parent_id, n.name, CAST(n.path_ids AS text) AS path_ids, n.flags,
       FALSE AS created
FROM file_nodes n
JOIN wanted w ON w.parent_id = n.parent_id AND w.name = n.name
WHERE n.org_team_id = :org_team_id
  AND n.trashed_at IS NULL
  AND n.id <> w.id
"""


@dataclass(frozen=True, slots=True)
class _Pending:
    """One folder this call proposes, before the level statement decides its id."""

    path: str
    id: uuid.UUID
    ino: int
    parent: uuid.UUID
    name: bytes
    path_ids: str
    #: What the folder is born wearing: the restricting half of its parent's
    #: bits, and the record mark when it is a chat's runtime directory. Decided
    #: here, per row, because a level holds folders of several parents.
    flags: int


@dataclass(frozen=True, slots=True)
class _Parent:
    """What a level needs to know about the folder each new one is made in."""

    path_ids: str
    flags: int
    subtype: str | None


@dataclass(frozen=True, slots=True)
class TreeResult:
    """Every folder of the skeleton by its relative path, and which are new.

    `created` is the subset this call actually inserted, so a replay's is empty
    — the caller's proof that the second attempt changed nothing.
    """

    nodes: dict[str, NodeId]
    created: frozenset[str]
    idempotency_key: str

    @property
    def replayed(self) -> bool:
        return not self.created


def prefixes(paths: Sequence[str]) -> list[str]:
    """Every distinct folder a set of relative paths implies, shallowest first.

    ``["a/b/c.txt", "a/d/e.txt"]`` implies ``a``, ``a/b`` and ``a/d`` — the
    file leaf is not a folder and never appears. Sorting by depth is what makes
    the levels sequential: a level's parents all exist once the level above ran.
    """
    wanted: set[str] = set()
    for raw in paths:
        segments = [segment for segment in raw.split("/") if segment != ""]
        # The last segment is the dropped file itself; only its ancestors are
        # folders. A path ending in "/" names a folder, and keeps all of them.
        folders = segments if raw.endswith("/") else segments[:-1]
        for depth in range(len(folders)):
            wanted.add("/".join(folders[: depth + 1]))
    return sorted(wanted, key=lambda p: (p.count("/"), p))


async def create_tree(
    repo: FilesRepo,
    ctx: ActingContext,
    drive_id: DriveId,
    parent_id: NodeId,
    paths: Sequence[str],
    *,
    idempotency_key: str,
    lease: LeaseContext | None = None,
    checkpoints: Checkpoints = _NO_CHECKPOINTS,
) -> TreeResult:
    """Create every folder `paths` implies under `parent_id`, in one round trip."""
    if not idempotency_key:
        raise InvalidRequest("files.idempotency_key_required", "create_tree needs a key")
    wanted = prefixes(paths)
    if not wanted:
        return TreeResult(nodes={}, created=frozenset(), idempotency_key=idempotency_key)
    if len(wanted) > MAX_TREE_FOLDERS:
        raise InvalidRequest(
            "files.tree_too_large", f"{len(wanted)} folders exceeds {MAX_TREE_FOLDERS}"
        )
    deepest = max(path.count("/") for path in wanted) + 1
    if deepest > MAX_TREE_DEPTH:
        raise InvalidRequest("files.tree_too_deep", f"{deepest} levels exceeds {MAX_TREE_DEPTH}")

    # Validate every segment before a single row is written: a skeleton is all
    # or nothing, and the naming contract is the only thing that can refuse one.
    encoded: dict[str, bytes] = {}
    for path in wanted:
        segment = path.rpartition("/")[2].encode()
        try:
            names.validate(segment)
        except names.InvalidName as exc:
            # The same code a create or a rename leaves as: a skeleton raised a
            # bare ``too_long`` that no client table has a row for, so a folder
            # upload with one bad segment read as "Request failed" where New
            # folder named the rule.
            raise InvalidRequest(f"files.invalid_name.{exc.code}", str(exc)) from exc
        encoded[path] = segment

    root = await repo.node(parent_id)
    if root is None or root.drive_id != drive_id:
        raise NotFound()
    if root.trashed_at is not None:
        raise Conflict("files.trashed", "the parent is in the trash")
    # Every folder this writes lands inside ``root``, so the skeleton is fenced
    # against the lease covering ``root`` the way a single create is: raising a
    # tree inside somebody else's mount is their write to make.
    await fenced_write_for(repo, root, lease)

    reserved = iter(await _reserve_inos(repo, drive_id, count=len(wanted)))
    await checkpoints.reach("tree.after_ino_block")

    made: dict[str, NodeId] = {}
    parents: dict[str, _Parent] = {str(parent_id): _Parent(root.path_ids, root.flags, root.subtype)}
    created: set[str] = set()
    by_level: dict[int, list[str]] = {}
    for path in wanted:
        by_level.setdefault(path.count("/"), []).append(path)

    who = await owner_ref(repo, ctx, drive_id, root.path_ids)
    for level in sorted(by_level):
        rows: list[_Pending] = []
        for path in by_level[level]:
            head, _, _leaf = path.rpartition("/")
            parent = str(made[head]) if head else str(parent_id)
            above = parents[parent]
            ino = next(reserved)
            rows.append(
                _Pending(
                    path=path,
                    id=uuid.uuid4(),
                    ino=ino,
                    parent=uuid.UUID(parent),
                    name=encoded[path],
                    path_ids=f"{above.path_ids}.{ino_label(ino)}",
                    flags=flags_for_child(
                        above.flags, parent_subtype=above.subtype, name=encoded[path]
                    ),
                )
            )
        placed = await _insert_level(
            repo,
            drive_id,
            rows,
            depth=root.depth + level + 1,
            created_by=who,
        )
        for row in rows:
            node_id, was_created, path_ids, flags = placed[row.name, row.parent]
            made[row.path] = node_id
            # A folder the skeleton makes is a plain folder; one it walked into
            # keeps whatever it was born with, which the statement read back.
            parents[str(node_id)] = _Parent(path_ids, flags, None)
            if was_created:
                created.add(row.path)
        await checkpoints.reach("tree.after_level")

    for path in sorted(created):
        node_id = made[path]
        await history.record(
            repo,
            ctx,
            node_id=node_id,
            kind="create",
            before=None,
            after={"kind": "folder", "idempotency_key": idempotency_key},
        )
        await history.emit_node_changed(repo, ctx, node_id=node_id, drive_id=drive_id, version=1)
    await checkpoints.reach("tree.after_history")
    return TreeResult(nodes=made, created=frozenset(created), idempotency_key=idempotency_key)


def _name_flags(name: bytes) -> str:
    """The computed-at-create name warnings, as the jsonb the column holds."""
    computed = names.flags(name)
    return json.dumps(
        {
            "windows_safe": computed.windows_safe,
            "display_warning": computed.display_warning,
        }
    )


async def _reserve_inos(repo: FilesRepo, drive_id: DriveId, *, count: int) -> list[int]:
    """Take whole blocks from the same allocator a single create uses.

    Bumping `next_ino` by the exact number of folders publishes that number: a
    caller who reads the counter before and after a stranger's drop learns how
    many folders the stranger made. The allocator only ever moves the counter in
    fixed blocks, so the gap says how many blocks were needed and nothing more.
    The blocks it hands back need not be one run — a concurrent reserver on the
    same drive can land between two of them — so the inos are carried per row.
    """
    allocator = InoAllocator(repo)
    try:
        return [await allocator.allocate(drive_id) for _ in range(count)]
    except LookupError as exc:
        raise NotFound() from exc


async def _insert_level(
    repo: FilesRepo,
    drive_id: DriveId,
    rows: Sequence[_Pending],
    *,
    depth: int,
    created_by: uuid.UUID,
) -> dict[tuple[bytes, uuid.UUID], tuple[NodeId, bool, str, int]]:
    """One statement for one level: insert the missing, read back all of them."""
    repo._require_open()
    statement = text(_LEVEL_SQL).bindparams(
        bindparam("ids"),
        bindparam("inos"),
        bindparam("parents"),
        bindparam("names"),
        bindparam("displays"),
        bindparam("keys"),
        bindparam("paths"),
        bindparam("name_flags"),
        bindparam("flags"),
    )
    result = await repo.session.execute(
        statement,
        {
            "ids": [row.id for row in rows],
            "inos": [row.ino for row in rows],
            "parents": [row.parent for row in rows],
            "names": [row.name for row in rows],
            "displays": [names.display(row.name) for row in rows],
            "keys": [names.name_key(row.name) for row in rows],
            "paths": [row.path_ids for row in rows],
            "name_flags": [_name_flags(row.name) for row in rows],
            "flags": [row.flags for row in rows],
            "drive_id": drive_id,
            "org_team_id": repo.scope.org_team_id,
            "depth": depth,
            "created_by": created_by,
        },
    )
    placed: dict[tuple[bytes, uuid.UUID], tuple[NodeId, bool, str, int]] = {}
    for found in result:
        key = (bytes(found.name), found.parent_id)
        # An existing row wins over the one this call proposed: the conflicting
        # insert wrote nothing, so its id is not the id anybody else will see.
        if key not in placed or not found.created:
            placed[key] = (NodeId(found.id), bool(found.created), found.path_ids, int(found.flags))
    return placed


__all__ = [
    "MAX_TREE_DEPTH",
    "MAX_TREE_FOLDERS",
    "TreeResult",
    "create_tree",
    "prefixes",
]
