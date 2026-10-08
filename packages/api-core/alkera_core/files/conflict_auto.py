"""The drive settling a two-way write on its own, in the writer's transaction.

A folder a machine holds takes writes from both sides. When both moved one
file, the drive is the single place the two writes meet, so it is the one that
decides — and it decides the same way every time: **the last write to arrive
keeps the name, and the bytes it displaced are kept beside it** as a
conflicted copy the drive names. Two arrivals reach here:

* the holder's own upload over a head the web moved (``arrived_from="holder"``):
  the holder's bytes become the head and the web's become the copy, which the
  holder is then asked to download like any other inbound write;
* the holder's *conflict submission* (``arrived_from="web"``): the holder
  applied the web's write to its disk and hands back the bytes that write
  displaced there, which the drive files as the copy.

Nothing here ever drops bytes. The displaced version always stays a version in
the node's history, and — unless the path is machine-managed or the node
already has as many copies as the drive allows — a copy node holds it as its
head too. The copy's version is a NEW row pointing at the same stored object
(content-addressed, like a restore), so no byte moves in the store.

Kept apart from :mod:`alkera_core.files.conflicts` for the reason
:mod:`alkera_core.files.conflict_resolution` gives: that module is the pure
naming algorithm, and this one is transactional. The name itself still comes
from there — :func:`~alkera_core.files.conflicts.free_conflicted_copy_name` —
so a copy is spelled the same on every façade.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from sqlalchemy import CursorResult, select, text

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings as default_settings
from alkera_core.files.acl import as_login_role
from alkera_core.files.clock import Clock
from alkera_core.files.conflicts import free_conflicted_copy_name
from alkera_core.files.errors import PreconditionFailed, QuotaExceeded
from alkera_core.files.history import actor_ref, emit_node_changed, record, subject_ref
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import LeaseContext
from alkera_core.files.membership import active_member_of
from alkera_core.files.namespace import Namespace
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.retention import HEAD_ONLY, HEAD_ONLY_DIRS, label_for
from alkera_core.models.files.history import FileConflict
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.models.user import User

__all__ = [
    "ANONYMOUS_WRITER",
    "ArrivedFrom",
    "CopyMode",
    "CopyTarget",
    "conflict_copy_count",
    "holder_name",
    "machine_managed",
    "reserve_copy",
    "settle_displacement",
    "writer_name",
]

#: Which side's write reached the drive last and kept the name.
ArrivedFrom = Literal["web", "holder"]

#: Where the displaced bytes go: a fresh copy node, a new version of the
#: newest copy once the ceiling is reached, or nowhere but the node's own
#: history on a machine-managed path.
CopyMode = Literal["copy", "newest", "versions_only"]

#: What a copy is named after when nothing names the writer: a version written
#: by a principal that is neither a member nor a machine of this org.
ANONYMOUS_WRITER: Final = "someone"

#: The ``version_metadata`` keys and the columns a carried version copies from
#: the one it carries. The same bytes, so the same description of them; the
#: identity, the order and the provenance are the new row's own.
_CARRIED_METADATA: Final[tuple[str, ...]] = ("block_hashes", "dedup_domain_id")
_CARRIED: Final[tuple[str, ...]] = (
    "size_bytes",
    "content_hash",
    "block_hash",
    "manifest_id",
    "inline_bytes",
    "store_key",
    "mime_sniffed",
    "scan_state",
    "scanned_at",
    "scan_engine_version",
)


@dataclass(frozen=True, slots=True)
class CopyTarget:
    """Where one displacement will land, decided before any bytes are placed.

    ``node_id``/``name`` are the copy node's (a fresh one, or the newest one
    past the ceiling), and ``None`` for a versions-only resolution.
    """

    mode: CopyMode
    node_id: NodeId | None = None
    name: bytes | None = None

    def to_wire(self) -> dict[str, Any]:
        """The target as a JSON document an operation row can carry."""
        return {
            "mode": self.mode,
            "node_id": None if self.node_id is None else str(self.node_id),
            "name": None if self.name is None else self.name.hex(),
        }

    @classmethod
    def from_wire(cls, document: dict[str, Any]) -> CopyTarget:
        """The inverse of :meth:`to_wire`."""
        raw_node = document.get("node_id")
        raw_name = document.get("name")
        mode = document.get("mode")
        return cls(
            mode=cast(CopyMode, mode if mode in ("copy", "newest") else "versions_only"),
            node_id=None if not raw_node else NodeId(uuid.UUID(str(raw_node))),
            name=None if not raw_name else bytes.fromhex(str(raw_name)),
        )


async def machine_managed(repo: FilesRepo, node: FileNode) -> bool:
    """Whether ``node`` lives where a tool, not a person, writes the bytes.

    Two ways in, one answer: a folder above it carries one of the names in
    :data:`~alkera_core.files.retention.HEAD_ONLY_DIRS` (``.git``,
    ``node_modules`` …), or the retention label governing it is ``head-only``.
    The node's own name is not consulted: a FILE named ``dist`` is a person's.
    """
    chain = await repo.chain(node)
    ancestors = [row for row in chain if row.id != node.id]
    if any(bytes(row.name) in HEAD_ONLY_DIRS for row in ancestors):
        return True
    label = await label_for(repo, node, chain)
    return label is not None and label.name == HEAD_ONLY


async def holder_name(repo: FilesRepo, lease: Any) -> str:
    """The machine holding ``lease``, as a person reads it.

    A box registers under its allocation's id, whose own name
    (``build-box``, say) is what a person recognises; a laptop mount registers
    under a name of its own, which is used as it is. Scoped to this org: an id
    another tenant's allocation carries resolves to nothing, not to its name.
    """
    machine = str(getattr(lease, "machine_id", "") or "")
    try:
        allocation = uuid.UUID(machine)
    except ValueError:
        return machine or ANONYMOUS_WRITER
    # By table name rather than the compute model, whose module pulls in a
    # subsystem the Files library does not import.
    async with as_login_role(repo):
        named = (
            await repo.session.execute(
                text("SELECT name FROM compute_allocations WHERE id = :id AND org_team_id = :org"),
                {"id": allocation, "org": repo.scope.org_team_id},
            )
        ).scalar_one_or_none()
    return named or machine


async def writer_name(repo: FilesRepo, version: FileVersion | None) -> str:
    """Who wrote ``version``, as a person reads it.

    The member's display name, their email when they have not named themselves,
    and :data:`ANONYMOUS_WRITER` for anything else — a service, a folded agent
    id, a member of another org. Never the raw id: this text ends up in a file
    name. ``users`` is another subsystem's table, read under the login role the
    way the ACL's principal check reads it.
    """
    if version is None or version.created_by is None:
        return ANONYMOUS_WRITER
    async with as_login_role(repo):
        row = (
            await repo.session.execute(
                select(User.first_name, User.last_name, User.email).where(
                    User.id == version.created_by,
                    active_member_of(repo.scope.org_team_id),
                )
            )
        ).one_or_none()
    if row is None:
        return ANONYMOUS_WRITER
    display = f"{row.first_name} {row.last_name}".strip()
    return display or str(row.email)


async def conflict_copy_count(
    repo: FilesRepo, node_id: NodeId
) -> tuple[int, NodeId | None, bytes | None]:
    """How many live conflicted copies sit beside ``node_id``, and the newest.

    A copy is a live (untrashed) node some conflict row of this node names; a
    copy a person trashed no longer counts against the ceiling. Newest is the
    highest ``ino``, which a drive hands out in creation order.
    """
    rows = (
        await repo.session.execute(
            text(
                "SELECT n.id, n.name FROM file_nodes n "
                "WHERE n.org_team_id = :org AND n.trashed_at IS NULL "
                "AND n.id IN (SELECT copy_node_id FROM file_conflicts "
                "WHERE org_team_id = :org AND node_id = :node AND copy_node_id IS NOT NULL) "
                "ORDER BY n.ino DESC"
            ),
            {"org": repo.scope.org_team_id, "node": node_id},
        )
    ).all()
    if not rows:
        return 0, None, None
    return len(rows), NodeId(rows[0].id), bytes(rows[0].name)


async def reserve_copy(
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    *,
    node: FileNode,
    displaced_by: str,
    lease: LeaseContext | None,
    limit: int | None = None,
) -> CopyTarget:
    """Decide where ``node``'s displaced bytes go, minting the copy if one is due.

    Runs in the caller's transaction, which already holds the drive: the name
    is chosen and the node created under that lock, so the name the caller is
    told is the name the copy has. The copy is created empty — its head lands
    in :func:`settle_displacement`, once the bytes it will hold are a version.

    A drive at its ceiling refuses a new node; that displacement is kept as a
    version instead of refused, because refusing it would lose it.
    """
    if node.parent_id is None or await machine_managed(repo, node):
        return CopyTarget(mode="versions_only")
    ceiling = default_settings.files_conflict_copies_max if limit is None else limit
    count, newest, newest_name = await conflict_copy_count(repo, NodeId(node.id))
    if newest is not None and count >= max(ceiling, 1):
        return CopyTarget(mode="newest", node_id=newest, name=newest_name)
    if ceiling <= 0:
        return CopyTarget(mode="versions_only")
    taken = {bytes(row.name) for row in await repo.siblings(NodeId(node.parent_id))}
    name = free_conflicted_copy_name(
        bytes(node.name), displaced_by, clock.now(), taken.__contains__
    )
    try:
        copy = await Namespace(repo, ctx, clock).create(
            DriveId(node.drive_id), NodeId(node.parent_id), "file", name, lease=lease
        )
    except QuotaExceeded:
        return CopyTarget(mode="versions_only")
    await repo.session.execute(
        text(
            'UPDATE file_nodes SET "metadata" = coalesce("metadata", \'{}\'::jsonb) '
            "|| CAST(:meta AS jsonb) WHERE id = :node AND org_team_id = :org"
        ),
        {
            "meta": json.dumps({"conflict_of": str(node.id)}),
            "node": copy.id,
            "org": repo.scope.org_team_id,
        },
    )
    return CopyTarget(mode="copy", node_id=NodeId(copy.id), name=bytes(copy.name))


async def settle_displacement(
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    *,
    node: FileNode,
    displaced: FileVersion,
    kept: FileVersion | None,
    base: FileVersion | None,
    target: CopyTarget,
    arrived_from: ArrivedFrom,
    kept_by: str,
    displaced_by: str,
    conflict_id: uuid.UUID | None = None,
) -> FileConflict:
    """Place the displaced bytes where ``target`` says and put it on record.

    ``kept_by`` is who wrote the bytes that kept the name (the ``who`` the
    pane shows, beside ``arrived_from``); ``displaced_by`` whose bytes went to
    the copy, the name the copy's file already carries.

    ``displaced`` is a version of ``node`` itself — the head the holder's
    upload replaced, or the holder's submission parked beside the head — so
    the node's own history holds it whatever else happens. The copy, when there
    is one, gets a version of its own carrying the same bytes.

    Writes, in the caller's transaction: the copy's head (and its quota), a
    ``conflict`` history row on the node and on the copy, the ``auto`` conflict
    row, and a ``file_node.changed`` (reason ``conflict``) for each.
    """
    copy_id: NodeId | None = None
    if target.mode != "versions_only" and target.node_id is not None:
        copy = await repo.node(target.node_id)
        if copy is not None and copy.trashed_at is None:
            copy_id = NodeId(copy.id)
            await _fill_copy(
                repo, ctx, clock, copy=copy, source=displaced, mime_class=node.mime_class
            )
    row = FileConflict(
        id=conflict_id if conflict_id is not None else uuid.uuid4(),
        org_team_id=repo.scope.org_team_id,
        node_id=node.id,
        base_version_id=None if base is None else base.id,
        theirs_version_id=displaced.id,
        mine_version_id=(kept if kept is not None else displaced).id,
        actor=actor_ref(ctx),
        state="auto",
        copy_node_id=copy_id,
        arrived_from=arrived_from,
        who=kept_by[:255],
        displaced_by=displaced_by[:255],
    )
    await repo.add(row)
    await repo.flush()
    note = {
        "conflict_id": str(row.id),
        "kept": None if kept is None else str(kept.id),
        "displaced": str(displaced.id),
        "copy": None if copy_id is None else str(copy_id),
        "arrived_from": arrived_from,
        "who": kept_by,
        "displaced_by": displaced_by,
    }
    await record(repo, ctx, node_id=NodeId(node.id), kind="conflict", before=None, after=note)
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=int(node.etag),
        parent_id=None if node.parent_id is None else NodeId(node.parent_id),
        reason="conflict",
    )
    if copy_id is not None:
        await record(repo, ctx, node_id=copy_id, kind="conflict", before=None, after=note)
        copied = await repo.node(copy_id)
        if copied is not None:
            await emit_node_changed(
                repo,
                ctx,
                node_id=copy_id,
                drive_id=DriveId(copied.drive_id),
                version=int(copied.etag),
                parent_id=None if copied.parent_id is None else NodeId(copied.parent_id),
                reason="conflict",
            )
            if arrived_from == "holder":
                # The holder's own bytes won the name, so the web's went to a
                # file the machine has never seen: it takes it like any other
                # write the drive accepted into its folder.
                await LiveEntriesService(repo, ctx).accept_inbound(copied, kind="inbound")
    return row


async def _fill_copy(
    repo: FilesRepo,
    ctx: ActingContext,
    clock: Clock,
    *,
    copy: FileNode,
    source: FileVersion,
    mime_class: str | None,
) -> None:
    """Make ``source``'s bytes the head of ``copy`` as a version of its own.

    A new row pointing at the same stored object — the move a restore makes —
    so the store is not asked for a byte. The head moves by compare-and-swap on
    the etag read here, under the drive lock the caller holds.
    """
    seq = int(
        (
            await repo.session.execute(
                text(
                    "SELECT coalesce(max(seq), 0) + 1 FROM file_versions "
                    "WHERE node_id = :node AND org_team_id = :org"
                ),
                {"node": copy.id, "org": repo.scope.org_team_id},
            )
        ).scalar_one()
    )
    carried = FileVersion(
        id=uuid.uuid4(),
        org_team_id=repo.scope.org_team_id,
        node_id=copy.id,
        seq=seq,
        source="copy",
        version_metadata={
            **{
                key: source.version_metadata[key]
                for key in _CARRIED_METADATA
                if key in (source.version_metadata or {})
            },
            "copied_from": str(source.id),
        },
        created_by=source.created_by if source.created_by is not None else subject_ref(ctx),
        **{name: getattr(source, name) for name in _CARRIED},
    )
    await repo.add(carried)
    await repo.flush()
    before_etag = int(copy.etag)
    before_size = int(copy.size)
    first_head = copy.head_version_id is None
    result = cast(
        CursorResult[Any],
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET head_version_id = :version, size = :size, "
                "mime_class = :mime, etag = etag + 1, mtime_ns = :mtime "
                "WHERE id = :node AND org_team_id = :org AND etag = :etag"
            ),
            {
                "version": carried.id,
                "size": int(carried.size_bytes),
                "mime": mime_class,
                "mtime": int(clock.now().timestamp() * 1_000_000_000),
                "node": copy.id,
                "org": repo.scope.org_team_id,
                "etag": before_etag,
            },
        ),
    )
    if result.rowcount != 1:  # pragma: no cover - the drive lock serializes writers
        raise PreconditionFailed(message=f"node {copy.id} moved on")
    await repo.session.refresh(copy)
    await record(
        repo,
        ctx,
        node_id=NodeId(copy.id),
        kind="attrs",
        before={"etag": before_etag, "size": before_size},
        after={"etag": before_etag + 1, "size": int(carried.size_bytes)},
    )
    if copy.parent_id is not None:
        await QuotaService(repo, ctx, clock).settle_head_swap(
            DriveId(copy.drive_id),
            NodeId(copy.parent_id),
            bytes_delta=int(carried.size_bytes) - before_size,
            files_delta=1 if first_head else 0,
        )
