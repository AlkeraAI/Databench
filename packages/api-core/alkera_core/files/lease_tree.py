"""The holder's tree report: the folder's rows, minted ahead of their bytes.

A machine holding a folder reports the tree itself (every name, kind, size
and modified time, a few hundred milliseconds after the disk changed), and this
module turns each report into rows. Waiting for uploads instead would leave a
folder watched while an agent clones a repository empty for a minute and then
full. The
bytes follow on their own path (the fenced upload); until they land a row says
what the disk holds through its holder facet (``holder_size`` and friends,
:mod:`alkera_core.files.freshness`).

One batch is one transaction, and the order is the contract:

1. **The leased folder, then the fence -- and the namespace only when needed.**
   The batch locks the leased folder (the outermost live-leased folder at or
   above it, ``FilesRepo.lock_lease_gate``), which every other writer under
   the lease takes before any row inside it, and then fences the lease row at
   update strength -- this epoch, this instance, this caller -- before a
   single row is read or written, so a superseded holder's batch changes
   nothing: no row, no ``live_seq`` bump, no announcement. It takes no
   namespace lock, so a person's create or move elsewhere in the org never
   queues behind it. A batch that turns out to trash or move something needs
   the drive's namespace exclusively (``FilesRepo.lock_namespace``), which the
   fixed order puts before the folder: it learns that inside a savepoint,
   rolls the savepoint back (letting go of the folder and the lease), takes
   the namespace, and runs again from the top.
2. **The batch id.** A resend of a batch already applied answers the first
   answer and does nothing, for as long as the idempotency store keeps keys.
3. **Containment, lexically.** Every path must name something under the lease
   root: no ``.``/``..``/empty segment, no leading ``/``, nothing the machine
   keeps to itself, no pointer name, nothing the naming contract refuses. One
   bad entry refuses the whole batch, naming the offending paths, so a holder
   drops them and resends the rest -- and cannot probe outside its folder one
   entry at a time.
4. **Deletes, deepest first.** A file with no bytes is removed outright; a file
   with bytes, and a folder, go to the trash the ordinary way under the
   holder's identity, so their history and their undo keep working.
5. **Renames.** The ordinary move and rename, under the fence, for a source
   the drive knows; a source it does not know is an upsert of the destination.
   They come before the creations so that a batch naming children of a
   renamed folder's new name lands them in the folder that was renamed rather
   than in a new one of the same name.
6. **Creations and reports.** Every missing folder on the way to a reported
   path and every missing file, in one INSERT; every existing file's report in
   one UPDATE. A report that proves the drive current clears the facet instead
   of setting it -- a facet exists only while bytes are on their way.
7. **The sequence, the count, the frames.** ``live_seq`` bumped and the lease
   marked synced; the ceiling on reported files checked; one
   ``file_node.changed`` per folder the batch touched (``live_batch``), or, past
   :data:`MAX_NAMED_FOLDERS`, none of them and ``subtree`` on the lease frame.

Statements are grouped per kind rather than per entry, so a two-thousand-entry
report of new files costs about twenty statements. Trashing and renaming go
through the ordinary paths and cost what those cost; a holder sends few of
them.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.locking import held_mark, savepoint_rolled_back
from alkera_core.files import names
from alkera_core.files.authz.decider import flags_for_child
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.digest import EMPTY, Digest, DigestChild, directory_digest
from alkera_core.files.errors import Conflict, TooLarge
from alkera_core.files.freshness import current_sql
from alkera_core.files.history import (
    LEASE_CHANGED_REPORT,
    emit_lease_changed,
    emit_node_changed,
)
from alkera_core.files.idempotency import StoredResponse, claim_key, settle_key
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import DEFAULT_BLOCK, InoAllocator, InoBlock, reserve_apart
from alkera_core.files.leases import (
    LeaseConflict,
    LeaseContext,
    assert_lease_epoch,
    holder_identity,
)
from alkera_core.files.namespace import HolderNode, Namespace, insert_holder_nodes
from alkera_core.files.path_labels import ino_label
from alkera_core.files.providers.registry import POINTER_EXTENSIONS
from alkera_core.files.quota import CeilingsResolver, QuotaService
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.files.stats import add_deltas
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.trash import Trash
from alkera_core.models.files.tree import FileNode

#: The idempotency store's route name for a tree report. The key is the lease
#: and the batch id together, so a batch id is remembered per lease.
TREE_IDEMPOTENCY_ROUTE: Final = "lease_tree.batch"
#: How many folders one batch announces by name. Past it the batch announces
#: none of them and marks the lease frame ``subtree``: a reader refetches every
#: folder it has open under the lease, once, instead of taking a frame per
#: folder of a clone.
MAX_NAMED_FOLDERS: Final = 32
#: How many offending paths a refused batch names. Enough to drop every bad
#: entry of any batch a holder actually sends; a bound so the refusal cannot be
#: made as large as the batch.
MAX_NAMED_PATHS: Final = 64
#: The modes a report that names none is given.
DEFAULT_FILE_MODE: Final = 0o644
DEFAULT_FOLDER_MODE: Final = 0o755

TreeOpKind = Literal["upsert", "rename", "delete"]
TreeNodeKind = Literal["file", "dir"]

_POINTER_SUFFIXES: Final[tuple[bytes, ...]] = tuple(
    extension.encode("utf-8") for extension in POINTER_EXTENSIONS.values()
)


def _never(_relative: bytes) -> bool:
    return False


class _NeedsDrive(Exception):  # noqa: N818 - control flow inside this module, never raised out
    """The batch reached a step that takes the drive row it does not hold."""


@dataclass(frozen=True, slots=True)
class TreeChange:
    """One entry of a holder's report, in bytes.

    ``path`` and ``from_path`` are relative to the lease root with ``/``
    between segments and carry a name's bytes exactly; ``hash`` is the raw
    BLAKE3 digest.
    """

    op: TreeOpKind
    path: bytes
    kind: TreeNodeKind | None = None
    from_path: bytes | None = None
    size: int | None = None
    mtime_ns: int | None = None
    mode: int | None = None
    hash: bytes | None = None


@dataclass(frozen=True, slots=True)
class TreeAnswer:
    """What a batch did: the lease's sequence after it, how many entries it
    applied, and how many files under the lease still have bytes on the way."""

    live_seq: int
    applied: int
    landing_count: int

    def stored(self) -> StoredResponse:
        return StoredResponse(
            status=200,
            body=json.dumps(
                {
                    "live_seq": self.live_seq,
                    "applied": self.applied,
                    "landing_count": self.landing_count,
                }
            ).encode(),
        )

    @classmethod
    def from_stored(cls, stored: StoredResponse) -> TreeAnswer:
        body = json.loads(stored.body)
        return cls(
            live_seq=int(body["live_seq"]),
            applied=int(body["applied"]),
            landing_count=int(body["landing_count"]),
        )


@dataclass(frozen=True, slots=True)
class DigestReading:
    """What a digest request reads: each named folder's digest, and the names
    of the children of the folders it asked to list."""

    digests: dict[bytes, Digest] = field(default_factory=dict)
    children: dict[bytes, list[bytes]] = field(default_factory=dict)


@dataclass(slots=True)
class _Row:
    """An existing node a report names, as one statement read it."""

    id: uuid.UUID
    kind: str
    parent_id: uuid.UUID
    path_ids: str
    depth: int
    flags: int
    etag: int
    head_version_id: uuid.UUID | None
    state: str
    #: What the node is a folder OF (a chat), when it is one. A child minted
    #: directly under a chat folder is born a record when its name says so.
    subtype: str | None = None


@dataclass(frozen=True, slots=True)
class _Root:
    id: uuid.UUID
    drive_id: uuid.UUID
    path_ids: str
    depth: int
    flags: int
    subtype: str | None = None


@dataclass(slots=True)
class _Plan:
    """The batch after validation: deletes deepest first, renames in order,
    and the last word on every other path."""

    deletes: list[bytes] = field(default_factory=list)
    renames: list[TreeChange] = field(default_factory=list)
    upserts: dict[bytes, TreeChange] = field(default_factory=dict)


def _mismatch(paths: Iterable[bytes] = (), indexes: Iterable[int] = ()) -> LeaseConflict:
    """The whole-batch refusal. It names the offending entries -- by their
    position in the batch and by path -- so the holder drops exactly those and
    resends the rest; both are the holder's own words, so naming them tells it
    nothing it did not send."""
    named = [path.decode("utf-8", "replace") for path in list(paths)[:MAX_NAMED_PATHS]]
    positions = list(indexes)[:MAX_NAMED_PATHS]
    detail: dict[str, Any] = {}
    if named:
        detail["paths"] = named
    if positions:
        detail["indexes"] = positions
    return LeaseConflict(
        "files.lease_mismatch", "that path is not under this lease", detail=detail or None
    )


def _too_many(cap: int) -> LeaseConflict:
    return LeaseConflict(
        "files.live_too_many", f"the lease already has {cap} files waiting on their bytes"
    )


def _segments(path: bytes) -> list[bytes]:
    return path.split(b"/")


def _parent(path: bytes) -> bytes:
    head, _, _ = path.rpartition(b"/")
    return head


def _leaf(path: bytes) -> bytes:
    return path.rpartition(b"/")[2]


def _depth(path: bytes) -> int:
    return path.count(b"/") + 1


def _ancestors(path: bytes) -> list[bytes]:
    """Every folder above ``path`` under the root, shallowest first."""
    segments = _segments(path)
    return [b"/".join(segments[:depth]) for depth in range(1, len(segments))]


def _under(path: bytes, folder: bytes) -> bool:
    return path == folder or path.startswith(folder + b"/")


def _refused(path: bytes, local_state: Callable[[bytes], bool]) -> bool:
    """Whether ``path`` names anything but a place under the lease root."""
    if not path or path.startswith(b"/"):
        return True
    for segment in _segments(path):
        if segment in (b"", b".", b".."):
            return True
        try:
            names.validate(segment)
        except names.InvalidName:
            return True
    if local_state(path):
        return True
    leaf = _leaf(path)
    return any(leaf.endswith(suffix) and len(leaf) > len(suffix) for suffix in _POINTER_SUFFIXES)


def _digest(changes: Sequence[TreeChange]) -> bytes:
    """The batch as the idempotency store fingerprints it."""
    canonical = [
        [
            change.op,
            change.path.hex(),
            change.kind,
            change.from_path.hex() if change.from_path is not None else None,
            change.size,
            change.mtime_ns,
            change.mode,
            change.hash.hex() if change.hash is not None else None,
        ]
        for change in changes
    ]
    return hashlib.sha256(json.dumps(canonical).encode()).digest()


def plan_changes(
    changes: Sequence[TreeChange], *, local_state: Callable[[bytes], bool] = _never
) -> _Plan:
    """Validate a batch and order it, or refuse all of it.

    A path named twice means its last word. A rename onto its own source is a
    report of that path. Public so a caller can refuse a batch before it takes
    a lock -- the service does it again after its fence either way.
    """
    bad: dict[int, list[bytes]] = {}
    for index, change in enumerate(changes):
        for path in (change.path, change.from_path):
            if path is not None and _refused(path, local_state):
                bad.setdefault(index, []).append(path)
    if bad:
        raise _mismatch(
            dict.fromkeys(path for paths in bad.values() for path in paths), sorted(bad)
        )
    plan = _Plan()
    deletes: dict[bytes, None] = {}
    for change in changes:
        if change.op == "delete":
            plan.upserts.pop(change.path, None)
            deletes[change.path] = None
        elif change.op == "rename" and change.from_path != change.path:
            deletes.pop(change.path, None)
            plan.upserts.pop(change.path, None)
            plan.renames.append(change)
        else:
            deletes.pop(change.path, None)
            plan.upserts[change.path] = replace(change, op="upsert", from_path=None)
    plan.deletes = sorted(deletes, key=lambda path: (-_depth(path), path))
    return plan


class LeaseTreeService:
    """Applies a holder's tree reports to one org's leased folders."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        verified_machine_id: str | None = None,
        ceilings: CeilingsResolver | None = None,
        local_state: Callable[[bytes], bool] = _never,
        checkpoints: Checkpoints | None = None,
    ) -> None:
        self._repo = repo
        self._checkpoints: Checkpoints = (
            checkpoints if checkpoints is not None else NoopCheckpoints()
        )
        #: Whether this batch holds the drive's namespace exclusively. Trashing
        #: and moving take it, and the fixed order puts it before the leased
        #: folder, so a batch that holds the folder without it must start
        #: again rather than ask.
        self._holds_drive = False
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._ceilings = ceilings
        #: What the machine keeps to itself, relative to the folder. A seam
        #: because the rule lives with the per-workspace store the library may
        #: not import; the route hands it in, and a caller that has no such
        #: rule refuses nothing on its account.
        self._local_state = local_state
        self._identity = holder_identity(ctx, verified_machine_id=verified_machine_id)
        #: The inos this batch mints from, reserved before its first lock.
        self._inos: dict[uuid.UUID, InoBlock] = {}

    @property
    def _org(self) -> uuid.UUID:
        return self._repo.scope.org_team_id

    async def apply(
        self,
        lease_node_id: NodeId,
        *,
        drive_id: DriveId,
        epoch: int,
        instance_id: str,
        batch_id: uuid.UUID,
        changes: Sequence[TreeChange],
    ) -> TreeAnswer:
        """Apply one report and answer what it did. See the module docstring
        for the order; every refusal is raised before this returns, and the
        caller's transaction rolling back is what leaves no trace of it."""
        ceiling = settings.files_live_metadata_max_entries
        if len(changes) > ceiling:
            raise TooLarge(
                "files.batch_too_large", f"a tree report names at most {ceiling} entries"
            )
        await self._reserve_inos(drive_id, changes)
        self._holds_drive = False
        before = held_mark()
        savepoint = self._repo.session.begin_nested()
        await savepoint.start()
        try:
            answer = await self._apply_locked(
                lease_node_id,
                drive_id=drive_id,
                epoch=epoch,
                instance_id=instance_id,
                batch_id=batch_id,
                changes=changes,
            )
        except _NeedsDrive:
            # Rolling the savepoint back lets go of the folder and the lease
            # row it took, so the namespace can be taken first, in order.
            await savepoint.rollback()
            savepoint_rolled_back(before)
        except BaseException:
            if savepoint.is_active:
                await savepoint.rollback()
            raise
        else:
            await savepoint.commit()
            return answer
        await self._repo.lock_namespace(drive_id, exclusive=True)
        self._holds_drive = True
        return await self._apply_locked(
            lease_node_id,
            drive_id=drive_id,
            epoch=epoch,
            instance_id=instance_id,
            batch_id=batch_id,
            changes=changes,
        )

    async def _apply_locked(
        self,
        lease_node_id: NodeId,
        *,
        drive_id: DriveId,
        epoch: int,
        instance_id: str,
        batch_id: uuid.UUID,
        changes: Sequence[TreeChange],
    ) -> TreeAnswer:
        """The batch from its first lock on: the leased folder, the fence, then
        everything the module docstring lists after them."""
        await self._repo.lock_lease_gate(lease_node_id)
        await assert_lease_epoch(
            self._repo,
            lease_node_id,
            epoch,
            instance_id,
            holder=self._identity,
            lock="no key update",
        )
        root = await self._root(lease_node_id, drive_id)
        key = f"{lease_node_id}:{batch_id}"
        replayed = await claim_key(
            self._repo,
            self._ctx,
            route=TREE_IDEMPOTENCY_ROUTE,
            key=key,
            request_hash=_digest(changes),
        )
        if replayed is not None:
            return TreeAnswer.from_stored(replayed)
        plan = plan_changes(changes, local_state=self._local_state)
        lease = LeaseContext(epoch=epoch, instance_id=instance_id, holder=self._identity)
        answer = await self._apply(root, lease, plan, applied=len(changes))
        await settle_key(
            self._repo,
            self._ctx,
            route=TREE_IDEMPOTENCY_ROUTE,
            key=key,
            response=answer.stored(),
        )
        return answer

    def _need_drive(self) -> None:
        """Stop a batch that does not hold the namespace before a step that
        takes it (a trash or a move)."""
        if not self._holds_drive:
            raise _NeedsDrive

    async def _reserve_inos(self, drive_id: DriveId, changes: Sequence[TreeChange]) -> None:
        """Reserve every ino this batch can mint, committed apart, before it
        takes a lock.

        Reserving inside the batch would hold the drive row from that UPDATE to
        the batch's commit, queueing every other writer in the org behind it.
        A batch mints at most one row per path it reads the drive at, so that
        count bounds the block. A batch refused later wastes the block, which
        costs nothing; one that is refused now reserves nothing.
        """
        self._inos = {}
        try:
            plan = plan_changes(changes, local_state=self._local_state)
        except LeaseConflict:
            return
        if not plan.upserts and not plan.renames:
            return
        wanted = len(self._paths_for(plan))
        block = DEFAULT_BLOCK * max(1, math.ceil(wanted / DEFAULT_BLOCK))
        self._inos[drive_id] = await reserve_apart(self._repo, drive_id, block=block)

    async def digests(
        self,
        lease_node_id: NodeId,
        *,
        drive_id: DriveId,
        epoch: int,
        instance_id: str,
        paths: Sequence[bytes],
        list_children: Sequence[bytes] = (),
    ) -> DigestReading:
        """The drive's digest of each folder in ``paths``, for the holder's walk.

        Fenced like every call the holder makes, at share strength: it is a
        read, and it holds nothing else open. The folders are resolved and
        their children read in ONE statement whatever the count; a folder the
        drive does not have (or has as a file) digests as an empty one, which
        is what it holds. A file's facts are its holder facet while it has one
        and its landed bytes otherwise -- the same facts the holder hashes
        from its disk, so a folder the drive is current on agrees.

        ``list_children`` (each also one of ``paths``) are the folders whose
        children's names the walk wants too: it found them differing and must
        learn which of the drive's rows its disk no longer has. They cost ONE
        further statement whatever their number. A child the drive has queued
        for the holder to take -- or a folder with such a child anywhere
        beneath it -- is left out: the disk lacking it is the holder not having
        taken it yet, not a delete.
        """
        await assert_lease_epoch(
            self._repo,
            lease_node_id,
            epoch,
            instance_id,
            holder=self._identity,
            lock="share",
        )
        await self._root(lease_node_id, drive_id)
        bad = [path for path in paths if path and _refused(path, self._local_state)]
        if bad:
            raise _mismatch(dict.fromkeys(bad))
        if not paths:
            return DigestReading()
        wanted: dict[bytes, None] = {}
        for path in paths:
            if path:
                for ancestor in _ancestors(path):
                    wanted[ancestor] = None
                wanted[path] = None
        walk = list(wanted)
        rows = await self._repo.session.execute(
            text(
                "WITH RECURSIVE wanted AS ("
                "  SELECT * FROM unnest(CAST(:paths AS bytea[]), CAST(:parents AS bytea[]), "
                "  CAST(:names AS bytea[])) AS w(path, parent, name)"
                "), walk AS ("
                "  SELECT w.path, n.id FROM wanted w JOIN file_nodes n "
                "  ON n.parent_id = :root AND n.name = w.name AND n.org_team_id = :org "
                "  AND n.trashed_at IS NULL AND n.kind = 'folder' "
                "  WHERE w.parent = ''::bytea "
                "  UNION ALL "
                "  SELECT w.path, n.id FROM walk p JOIN wanted w ON w.parent = p.path "
                "  JOIN file_nodes n ON n.parent_id = p.id AND n.name = w.name "
                "  AND n.org_team_id = :org AND n.trashed_at IS NULL AND n.kind = 'folder'"
                "), folders AS ("
                "  SELECT ''::bytea AS path, CAST(:root AS uuid) AS id UNION ALL "
                "  SELECT path, id FROM walk"
                ") SELECT f.path, f.id, "
                "array_agg(c.kind) FILTER (WHERE c.id IS NOT NULL) AS kinds, "
                "array_agg(c.name) FILTER (WHERE c.id IS NOT NULL) AS names, "
                "array_agg(CASE WHEN c.kind = 'folder' THEN 0 "
                "  ELSE coalesce(c.holder_size, hv.size_bytes, 0) END) "
                "  FILTER (WHERE c.id IS NOT NULL) AS sizes, "
                "array_agg(CASE WHEN c.kind = 'folder' THEN 0 "
                "  ELSE coalesce(c.holder_mtime_ns, c.mtime_ns) END) "
                "  FILTER (WHERE c.id IS NOT NULL) AS mtimes "
                "FROM folders f LEFT JOIN file_nodes c ON c.parent_id = f.id "
                "AND c.org_team_id = :org AND c.trashed_at IS NULL "
                "AND c.kind IN ('file', 'folder') "
                "LEFT JOIN file_versions hv ON hv.id = c.head_version_id "
                "AND hv.org_team_id = :org "
                "GROUP BY f.path, f.id"
            ),
            {
                "org": self._org,
                "root": lease_node_id,
                "paths": walk,
                "parents": [_parent(path) for path in walk],
                "names": [_leaf(path) for path in walk],
            },
        )
        found: dict[bytes, Digest] = {}
        folder_ids: dict[bytes, uuid.UUID] = {}
        for row in rows:
            folder_ids[bytes(row.path)] = row.id
            found[bytes(row.path)] = directory_digest(
                DigestChild(
                    kind="dir" if kind == "folder" else "file",
                    name=bytes(name),
                    size=int(size),
                    mtime_ns=int(mtime),
                )
                for kind, name, size, mtime in zip(
                    row.kinds or [], row.names or [], row.sizes or [], row.mtimes or [], strict=True
                )
            )
        return DigestReading(
            digests={path: found.get(path, EMPTY) for path in paths},
            children=await self._children_of(
                lease_node_id, {path: folder_ids.get(path) for path in list_children}
            ),
        )

    async def _children_of(
        self, lease_node_id: NodeId, folders: Mapping[bytes, uuid.UUID | None]
    ) -> dict[bytes, list[bytes]]:
        """The names of each folder's live direct children, in one statement,
        leaving out what the drive still owes the holder (see :meth:`digests`)."""
        listed: dict[bytes, list[bytes]] = {path: [] for path in folders}
        ids = {folder_id: path for path, folder_id in folders.items() if folder_id is not None}
        if not ids:
            return listed
        rows = await self._repo.session.execute(
            text(
                "SELECT c.parent_id, c.name FROM file_nodes c "  # noqa: S608
                "WHERE c.org_team_id = :org AND c.parent_id = ANY(:ids) "
                "AND c.trashed_at IS NULL AND c.kind IN ('file', 'folder') "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM file_lease_live_entries e "
                "  JOIN file_nodes m ON m.id = e.node_id AND m.org_team_id = :org "
                "  WHERE e.org_team_id = :org AND e.lease_node_id = :lease "
                "  AND e.state IN ('inbound', 'inbound_delete', 'inbound_rename') "
                f"  AND {subtree_sql('m.path_ids', 'c.path_ids')}"
                ") ORDER BY c.parent_id, c.name"
            ),
            {"org": self._org, "ids": list(ids), "lease": lease_node_id, **SUBTREE_DEPTH_BIND},
        )
        for row in rows:
            listed[ids[row.parent_id]].append(bytes(row.name))
        return listed

    # ---- the steps ---------------------------------------------------------

    async def _apply(
        self, root: _Root, lease: LeaseContext, plan: _Plan, *, applied: int
    ) -> TreeAnswer:
        seq = await self._bump(root.id)
        touched: set[uuid.UUID] = set()
        rows = await self._resolve(root, self._paths_for(plan))

        await self._remove(
            root, lease, [path for path in plan.deletes if path in rows], rows, touched
        )

        moved = await self._rename(root, lease, plan, rows, touched)
        if moved:
            rows = await self._resolve(root, self._paths_for(plan))

        conflicting = self._conflicts(plan, rows)
        await self._remove(root, lease, conflicting, rows, touched)

        # What the drive already had, before this batch mints anything: those
        # rows take the report by update, the minted ones were born with it.
        known = {path: rows[path] for path in plan.upserts if path in rows}
        await self._create(root, plan, rows, touched, seq=seq)
        await self._report(plan, known, touched, seq=seq)
        await self._checkpoints.reach("lease_tree.rows_written")

        reported, landing = await self._counts(root)
        cap = settings.files_holder_max_nodes
        if reported > cap:
            raise _too_many(cap)
        await self._announce(root, touched, seq=seq, landing=landing)
        return TreeAnswer(live_seq=seq, applied=applied, landing_count=landing)

    def _paths_for(self, plan: _Plan) -> list[bytes]:
        """Every path the batch reads the drive at: each named path, each
        rename's source, and every folder above them."""
        wanted: dict[bytes, None] = {}
        named = [
            *plan.deletes,
            *plan.upserts,
            *(change.path for change in plan.renames),
            *(change.from_path for change in plan.renames if change.from_path is not None),
        ]
        for path in named:
            for ancestor in _ancestors(path):
                wanted[ancestor] = None
            wanted[path] = None
        return list(wanted)

    async def _root(self, lease_node_id: NodeId, drive_id: DriveId) -> _Root:
        """The leased folder, and whether its lease runs the live plane.

        A lease that took no live plane (a plain mount) gets its rows with
        their bytes, so a report under it is refused
        rather than leaving rows that would read as left behind the instant
        they were minted.
        """
        row = (
            await self._repo.session.execute(
                text(
                    "SELECT n.id, n.drive_id, CAST(n.path_ids AS text) AS path_ids, n.depth, "
                    "n.flags, n.kind, n.subtype, l.live_cadence FROM file_leases l "
                    "JOIN file_nodes n ON n.id = l.node_id AND n.org_team_id = l.org_team_id "
                    "WHERE l.org_team_id = :org AND l.node_id = :node"
                ),
                {"org": self._org, "node": lease_node_id},
            )
        ).one()
        if row.drive_id != drive_id or row.kind != "folder" or not row.live_cadence:
            raise _mismatch()
        return _Root(
            id=row.id,
            drive_id=row.drive_id,
            path_ids=row.path_ids,
            depth=row.depth,
            flags=row.flags,
            subtype=row.subtype,
        )

    async def _bump(self, lease_node_id: uuid.UUID) -> int:
        """The sequence bump and the synced stamp, as one statement on the row
        the fence already holds at update strength."""
        return int(
            (
                await self._repo.session.execute(
                    text(
                        "UPDATE file_leases SET live_seq = live_seq + 1, last_sync_at = now() "
                        "WHERE org_team_id = :org AND node_id = :node RETURNING live_seq"
                    ),
                    {"org": self._org, "node": lease_node_id},
                )
            ).scalar_one()
        )

    async def _resolve(self, root: _Root, paths: Sequence[bytes]) -> dict[bytes, _Row]:
        """Every live node at one of ``paths``, in one statement.

        A recursive walk down from the root by ``(parent_id, name)`` -- the
        live-name index, reached with equality alone, which is what the row
        security on ``file_nodes`` lets the planner use. A path whose parent is
        missing is simply not found, and neither is anything under it.
        """
        if not paths:
            return {}
        result = await self._repo.session.execute(
            text(
                "WITH RECURSIVE wanted AS ("
                "  SELECT * FROM unnest(CAST(:paths AS bytea[]), CAST(:parents AS bytea[]), "
                "  CAST(:names AS bytea[])) AS w(path, parent, name)"
                "), walk AS ("
                "  SELECT w.path, n.id, n.kind, n.parent_id, n.path_ids, n.depth, n.flags, "
                "         n.etag, n.head_version_id, n.state, n.subtype "
                "  FROM wanted w JOIN file_nodes n ON n.parent_id = :root AND n.name = w.name "
                "  AND n.org_team_id = :org AND n.trashed_at IS NULL "
                "  WHERE w.parent = ''::bytea "
                "  UNION ALL "
                "  SELECT w.path, n.id, n.kind, n.parent_id, n.path_ids, n.depth, n.flags, "
                "         n.etag, n.head_version_id, n.state, n.subtype "
                "  FROM walk p JOIN wanted w ON w.parent = p.path "
                "  JOIN file_nodes n ON n.parent_id = p.id AND n.name = w.name "
                "  AND n.org_team_id = :org AND n.trashed_at IS NULL"
                ") SELECT path, id, kind, parent_id, CAST(path_ids AS text) AS path_ids, depth, "
                "flags, etag, head_version_id, state, subtype FROM walk"
            ),
            {
                "org": self._org,
                "root": root.id,
                "paths": list(paths),
                "parents": [_parent(path) for path in paths],
                "names": [_leaf(path) for path in paths],
            },
        )
        return {
            bytes(row.path): _Row(
                id=row.id,
                kind=row.kind,
                parent_id=row.parent_id,
                path_ids=row.path_ids,
                depth=row.depth,
                flags=row.flags,
                etag=row.etag,
                head_version_id=row.head_version_id,
                state=row.state,
                subtype=row.subtype,
            )
            for row in result
        }

    def _conflicts(self, plan: _Plan, rows: Mapping[bytes, _Row]) -> list[bytes]:
        """Paths whose existing node is not the kind the report needs there.

        A report is the disk's word: a folder where a file used to be means the
        file is gone. Every folder on the way to a reported path must be a
        folder, and every reported path must be the kind reported.
        """
        wanted: dict[bytes, str] = {
            path: "folder" if change.kind == "dir" else "file"
            for path, change in plan.upserts.items()
        }
        for path in plan.upserts:
            for ancestor in _ancestors(path):
                wanted[ancestor] = "folder"
        return [path for path, kind in wanted.items() if path in rows and rows[path].kind != kind]

    async def _remove(
        self,
        root: _Root,
        lease: LeaseContext,
        paths: Sequence[bytes],
        rows: dict[bytes, _Row],
        touched: set[uuid.UUID],
    ) -> None:
        """Delete these paths the way the disk lost them, deepest first.

        A file whose bytes never landed has nothing to keep and nothing to
        undo, so its row goes outright, in one statement for all of them. A
        file with bytes and a folder go to the trash through the ordinary path,
        under the holder's identity and fence. What goes takes everything
        resolved beneath it out of ``rows``, so nothing later in the batch
        builds on a node that is no longer there.
        """
        doomed = sorted(
            {path for path in paths if path in rows}, key=lambda path: (-_depth(path), path)
        )
        if not doomed:
            return
        headless = [
            rows[path].id
            for path in doomed
            if rows[path].kind == "file" and rows[path].head_version_id is None
        ]
        gone = await self._delete_headless(headless)
        for parent_id in gone.values():
            touched.add(parent_id)
        await add_deltas(
            self._repo,
            _count_by(gone.values(), sign=-1),
            child_change_at=self._clock.now(),
        )
        if any(rows[path].id not in gone for path in doomed):
            self._need_drive()
        trash = Trash(self._repo, self._ctx, self._clock, self._store)
        for path in doomed:
            row = rows[path]
            if row.id not in gone:
                await trash.trash(NodeId(row.id), if_match=row.etag, lease=lease)
                touched.add(row.parent_id)
            for other in [known for known in rows if _under(known, path)]:
                del rows[other]

    async def _delete_headless(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        """Remove files that never had bytes, and everything pointing at them,
        in ONE statement; answer ``{id: parent}`` for each row removed.

        A row is removed only if it still has no version of any kind and no
        hold: anything else is left for the trash, which knows how to keep it.
        Every table with a foreign key to the node is cleared in the same
        statement -- the key is checked at its end, so the order of the parts
        does not matter.
        """
        if not ids:
            return {}
        rows = await self._repo.session.execute(
            text(
                "WITH victims AS ("
                "  SELECT n.id FROM file_nodes n WHERE n.org_team_id = :org "
                "  AND n.id = ANY(:ids) AND n.kind = 'file' AND n.head_version_id IS NULL "
                "  AND n.retention_label_id IS NULL "
                "  AND NOT EXISTS (SELECT 1 FROM file_versions v "
                "                  WHERE v.org_team_id = :org AND v.node_id = n.id) "
                "  AND NOT EXISTS (SELECT 1 FROM file_holds h "
                "                  WHERE h.org_team_id = :org AND h.node_id = n.id)"
                "), history AS ("
                "  DELETE FROM file_history WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), live AS ("
                "  DELETE FROM file_lease_live_entries WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), uploads AS ("
                "  DELETE FROM file_upload_sessions WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), links AS ("
                "  DELETE FROM file_links WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), locks AS ("
                "  DELETE FROM file_locks WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), conflicts AS ("
                "  DELETE FROM file_conflicts WHERE org_team_id = :org "
                "  AND node_id IN (SELECT id FROM victims)"
                "), results AS ("
                "  UPDATE file_ops SET result_node_id = NULL WHERE org_team_id = :org "
                "  AND result_node_id IN (SELECT id FROM victims)"
                "), shortcuts AS ("
                "  UPDATE file_nodes SET target_id = NULL WHERE org_team_id = :org "
                "  AND target_id IN (SELECT id FROM victims)"
                ") DELETE FROM file_nodes WHERE org_team_id = :org "
                "AND id IN (SELECT id FROM victims) RETURNING id, parent_id"
            ),
            {"org": self._org, "ids": list(ids)},
        )
        return {row.id: row.parent_id for row in rows}

    async def _rename(
        self,
        root: _Root,
        lease: LeaseContext,
        plan: _Plan,
        rows: dict[bytes, _Row],
        touched: set[uuid.UUID],
    ) -> bool:
        """Apply the renames whose source the drive knows; turn the rest into
        reports of their destination. Answers whether anything moved.

        Each rename is the ordinary move and rename, fenced, so history, undo
        and the path rewrite of a moved folder's subtree are the same as any
        other. A destination that is taken is removed first -- the disk's
        rename replaced it -- and a destination folder that does not exist yet
        is created on the way.
        """
        namespace = Namespace(self._repo, self._ctx, self._clock, self._store)
        moved = False
        for change in plan.renames:
            assert change.from_path is not None
            source = rows.get(change.from_path)
            if source is None:
                plan.upserts[change.path] = replace(change, op="upsert", from_path=None)
                continue
            taken = rows.get(change.path)
            if taken is not None and taken.id != source.id:
                await self._remove(root, lease, [change.path], rows, touched)
            parent_id = await self._folder_at(root, _parent(change.path), rows, touched)
            node = source
            etag = source.etag
            if node.parent_id != parent_id:
                self._need_drive()
                made = await namespace.move(
                    NodeId(node.id), NodeId(parent_id), if_match=etag, lease=lease
                )
                # A folder too large to move inline is queued; its name is the
                # queued move's to settle, and the next report says it again.
                if not isinstance(made, FileNode):
                    continue
                etag = made.etag
            if _leaf(change.path) != _leaf(change.from_path):
                await namespace.rename(
                    NodeId(node.id), _leaf(change.path), if_match=etag, lease=lease
                )
            touched.update({source.parent_id, parent_id})
            moved = True
            # What the disk said about the file arrives with its rename, and is
            # reported like any other entry once the node stands at its path.
            plan.upserts[change.path] = replace(change, op="upsert", from_path=None)
            if source.kind == "folder":
                # Everything under a renamed folder now answers to its new
                # path, and a later rename in this batch may name it there.
                fresh = await self._resolve(root, self._paths_for(plan))
                rows.clear()
                rows.update(fresh)
            else:
                rows.pop(change.from_path, None)
        return moved

    async def _folder_at(
        self, root: _Root, path: bytes, rows: dict[bytes, _Row], touched: set[uuid.UUID]
    ) -> uuid.UUID:
        """The folder at ``path``, creating it and the folders above it if the
        drive does not have them yet."""
        if not path:
            return root.id
        missing = [folder for folder in [*_ancestors(path), path] if folder not in rows]
        if missing:
            await self._mint(root, missing, {}, rows, touched, seq=None)
        return rows[path].id

    async def _create(
        self,
        root: _Root,
        plan: _Plan,
        rows: dict[bytes, _Row],
        touched: set[uuid.UUID],
        *,
        seq: int,
    ) -> None:
        """Every folder on the way to a reported path, and every reported file,
        that the drive does not have yet."""
        folders: dict[bytes, None] = {}
        files: dict[bytes, TreeChange] = {}
        for path, change in plan.upserts.items():
            for ancestor in _ancestors(path):
                folders[ancestor] = None
            if change.kind == "dir":
                folders[path] = None
            else:
                files[path] = change
        # A path something else in the batch lives under is a folder, whatever
        # an earlier entry said it was.
        files = {path: change for path, change in files.items() if path not in folders}
        missing = [path for path in folders if path not in rows]
        missing_files = {path: change for path, change in files.items() if path not in rows}
        await self._mint(root, missing, missing_files, rows, touched, seq=seq)

    async def _mint(
        self,
        root: _Root,
        folders: Sequence[bytes],
        files: Mapping[bytes, TreeChange],
        rows: dict[bytes, _Row],
        touched: set[uuid.UUID],
        *,
        seq: int | None,
    ) -> None:
        paths = sorted({*folders, *files}, key=lambda path: (_depth(path), path))
        if not paths:
            return
        drive_id = DriveId(root.drive_id)
        # Read, not locked: the batch holds no drive row. Two batches -- or a
        # batch and a create that does hold the drive -- can each read usage
        # before the other's rows commit and both pass, so the node ceiling can
        # be overshot by at most one batch (files_live_metadata_max_entries
        # rows) per writer in flight at that instant. That is accepted: the
        # ceiling is a safety mode that refuses the NEXT write once usage is at
        # it, not an exact bound, and deciding it exactly would put the drive
        # row back in front of every batch.
        await QuotaService(
            self._repo, self._ctx, self._clock, self._store, ceilings=self._ceilings
        ).assert_room(drive_id, bytes=0, nodes=len(paths), parent_path=root.path_ids)
        # The block reserved before the batch's first lock covers every row it
        # can mint; one that runs out reserves more apart, which never writes
        # the drive row in this transaction.
        block = DEFAULT_BLOCK * max(1, math.ceil(len(paths) / DEFAULT_BLOCK))
        allocator = InoAllocator(self._repo, block=block, cache=self._inos)
        minted: list[HolderNode] = []
        for path in paths:
            parent_path = _parent(path)
            if parent_path:
                parent = rows[parent_path]
                parent_id, parent_ids, parent_depth, parent_flags, parent_subtype = (
                    parent.id,
                    parent.path_ids,
                    parent.depth,
                    parent.flags,
                    parent.subtype,
                )
            else:
                parent_id, parent_ids, parent_depth, parent_flags, parent_subtype = (
                    root.id,
                    root.path_ids,
                    root.depth,
                    root.flags,
                    root.subtype,
                )
            if parent_path and rows[parent_path].state == "moving":
                raise Conflict("files.moving", "a move of this subtree is in progress")
            ino = await allocator.allocate(drive_id)
            change = files.get(path)
            node = HolderNode(
                id=uuid.uuid4(),
                ino=ino,
                parent_id=parent_id,
                kind="file" if change is not None else "folder",
                name=_leaf(path),
                path_ids=f"{parent_ids}.{ino_label(ino)}",
                depth=parent_depth + 1,
                flags=flags_for_child(
                    parent_flags, parent_subtype=parent_subtype, name=_leaf(path)
                ),
                mode=(
                    change.mode
                    if change is not None and change.mode is not None
                    else DEFAULT_FILE_MODE
                    if change is not None
                    else DEFAULT_FOLDER_MODE
                ),
                mtime_ns=change.mtime_ns if change is not None else None,
                holder_size=(change.size or 0) if change is not None else None,
                holder_mtime_ns=(change.mtime_ns or 0) if change is not None else None,
                holder_hash=change.hash if change is not None else None,
            )
            minted.append(node)
            rows[path] = _Row(
                id=node.id,
                kind=node.kind,
                parent_id=parent_id,
                path_ids=node.path_ids,
                depth=node.depth,
                flags=node.flags,
                etag=1,
                head_version_id=None,
                state="live",
            )
            touched.add(parent_id)
        await insert_holder_nodes(
            self._repo,
            self._ctx,
            drive_id,
            minted,
            holder_seq=seq if seq is not None else 0,
            now=self._clock.now(),
        )

    async def _report(
        self,
        plan: _Plan,
        rows: Mapping[bytes, _Row],
        touched: set[uuid.UUID],
        *,
        seq: int,
    ) -> None:
        """Write what the disk says onto every file the drive already had, in
        ONE statement.

        A report that shows the drive already holds these bytes clears the
        facet rather than setting it -- the hash when both sides know it, else
        the size and the modified time -- and takes the disk's modified time
        onto the row. A report of the same size and time without a hash keeps
        the hash an earlier report carried; any other report drops it, because
        it described bytes the disk no longer holds. The row's own modified
        time follows the disk while it has no bytes, and stays the landed
        version's once it has.
        """
        reported = [
            (rows[path], change)
            for path, change in plan.upserts.items()
            if change.kind == "file" and path in rows and rows[path].kind == "file"
        ]
        if not reported:
            return
        result = await self._repo.session.execute(
            text(
                "WITH v AS ("
                "  SELECT * FROM unnest(CAST(:ids AS uuid[]), CAST(:sizes AS bigint[]), "
                "  CAST(:mtimes AS bigint[]), CAST(:hashes AS bytea[]), CAST(:modes AS integer[])) "
                "  AS v(id, size, mtime, hash, mode)"
                "), c AS ("
                "  SELECT v.id, v.size, v.mtime, v.mode, "
                "    coalesce(v.hash, CASE WHEN n.holder_size = v.size "
                "      AND n.holder_mtime_ns = v.mtime THEN n.holder_hash END) AS hash, "
                "    n.head_version_id, n.mtime_ns AS row_mtime, hv.size_bytes, hv.content_hash "
                "  FROM v JOIN file_nodes n ON n.id = v.id AND n.org_team_id = :org "
                "  LEFT JOIN file_versions hv ON hv.id = n.head_version_id "
                "  AND hv.org_team_id = :org"
                "), d AS ("
                "  SELECT c.*, (c.head_version_id IS NOT NULL AND CASE "
                "    WHEN c.hash IS NOT NULL AND c.content_hash <> '' "
                "    THEN c.content_hash = encode(c.hash, 'hex') "
                "    ELSE c.size_bytes = c.size AND c.row_mtime = c.mtime END) AS cur FROM c"
                ") UPDATE file_nodes AS n SET "
                "holder_size = CASE WHEN d.cur THEN NULL ELSE d.size END, "
                "holder_mtime_ns = CASE WHEN d.cur THEN NULL ELSE d.mtime END, "
                "holder_hash = CASE WHEN d.cur THEN NULL ELSE d.hash END, "
                "holder_seq = CASE WHEN d.cur THEN NULL ELSE CAST(:seq AS bigint) END, "
                "mode = coalesce(d.mode, n.mode), "
                "mtime_ns = CASE WHEN n.head_version_id IS NULL OR d.cur THEN d.mtime "
                "ELSE n.mtime_ns END, "
                "updated_at = now() "
                "FROM d WHERE n.id = d.id AND n.org_team_id = :org RETURNING n.parent_id"
            ),
            {
                "org": self._org,
                "seq": seq,
                "ids": [row.id for row, _ in reported],
                "sizes": [change.size or 0 for _, change in reported],
                "mtimes": [change.mtime_ns or 0 for _, change in reported],
                "hashes": [change.hash for _, change in reported],
                "modes": [change.mode for _, change in reported],
            },
        )
        touched.update(row.parent_id for row in result)

    async def _counts(self, root: _Root) -> tuple[int, int]:
        """How many files under the lease carry a report, and how many of those
        the drive does not hold current bytes for -- in one statement."""
        row = (
            await self._repo.session.execute(
                text(
                    "SELECT count(*) AS reported, "  # noqa: S608 - interpolates builders' own text
                    f"count(*) FILTER (WHERE NOT {current_sql('n', 'hv')}) AS landing "
                    "FROM file_nodes n LEFT JOIN file_versions hv "
                    "ON hv.id = n.head_version_id AND hv.org_team_id = :org "
                    "WHERE n.org_team_id = :org AND n.kind = 'file' AND n.trashed_at IS NULL "
                    "AND n.holder_size IS NOT NULL "
                    f"AND {subtree_sql('n.path_ids', 'CAST(:root AS ltree)')}"
                ),
                {"org": self._org, "root": root.path_ids, **SUBTREE_DEPTH_BIND},
            )
        ).one()
        return int(row.reported), int(row.landing)

    async def _announce(
        self, root: _Root, touched: set[uuid.UUID], *, seq: int, landing: int
    ) -> None:
        """One frame per folder the batch touched, or none and ``subtree``.

        Every frame is added without a flush and the lot written by one, so a
        batch that touched thirty folders costs one statement, not thirty.
        """
        drive_id = DriveId(root.drive_id)
        named = sorted(touched) if len(touched) <= MAX_NAMED_FOLDERS else []
        for folder in named:
            await emit_node_changed(
                self._repo,
                self._ctx,
                node_id=NodeId(folder),
                drive_id=drive_id,
                version=seq,
                parent_id=NodeId(folder),
                reason="live_batch",
                flush=False,
            )
        await emit_lease_changed(
            self._repo,
            self._ctx,
            lease_node_id=NodeId(root.id),
            drive_id=drive_id,
            live_seq=seq,
            landing_count=landing,
            subtree=len(touched) > MAX_NAMED_FOLDERS,
            reason=LEASE_CHANGED_REPORT,
            flush=False,
        )
        await self._repo.session.flush()


def _count_by(parents: Iterable[uuid.UUID], *, sign: int) -> dict[uuid.UUID, int]:
    counted: dict[uuid.UUID, int] = {}
    for parent in parents:
        counted[parent] = counted.get(parent, 0) + sign
    return counted


__all__ = [
    "DEFAULT_FILE_MODE",
    "DEFAULT_FOLDER_MODE",
    "MAX_NAMED_FOLDERS",
    "MAX_NAMED_PATHS",
    "TREE_IDEMPOTENCY_ROUTE",
    "DigestReading",
    "LeaseTreeService",
    "TreeAnswer",
    "TreeChange",
    "TreeNodeKind",
    "TreeOpKind",
    "plan_changes",
]
