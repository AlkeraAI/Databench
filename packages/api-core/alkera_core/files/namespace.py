"""The tree: create, rename, move, resolve, walk.

Three rules shape every statement here.

*The adjacency list is truth and ``path_ids`` is derived from it.* A create
computes the child's path from the parent's; a move rewrites the subtree's paths
from the moved node's new one. Nothing recomputes a path from names, and nothing
walks ``parent_id`` in Python to find a subtree — that is what the ltree is for.

*A conditional write is one statement.* A rename is one ``UPDATE … WHERE id = $n
AND etag = $e``; a move carries its cycle guard (``NOT (parent.path_ids <@
node.path_ids)``) in the same ``UPDATE``, reading the parent's path inside the
statement rather than from a value Python read earlier. Zero rows updated is the
answer, and which failure it was is decided by re-reading — never by checking
first and writing after.

*Locks are taken in one order, always: namespace → parent → node.* Two
mutations that took them in opposite orders would deadlock. The namespace is
the drive's tree shape (``FilesRepo.lock_namespace``): a create holds it
shared, so creates anywhere in the org go side by side, and a move holds it
exclusively, which is what makes two moves in one drive mutually exclusive (a
pair of moves that would each pass its own guard cannot interleave between the
guard and the write) and what keeps a create from deriving its path from a
folder a move is re-rooting.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alkera_core.authz.principal import ActingContext
from alkera_core.db.errors import is_unique_violation
from alkera_core.files import acl, history, names, stats
from alkera_core.files.attrs import stored_xattrs
from alkera_core.files.authz.decider import flags_for_child
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.conflicts import conflict_rename
from alkera_core.files.drives import assert_traversal_rules, home_owner
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.history import subject_ref
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import InoAllocator
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import (
    LeaseContext,
    admitted_inbound,
    fenced_write_for,
    is_final_push,
)
from alkera_core.files.links import stored_link_stays_inside
from alkera_core.files.path_labels import ino_label
from alkera_core.files.quota import CeilingsResolver, QuotaService
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.surfaces import decode_from_surface
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import SYMLINK_KINDS, FileNode

if TYPE_CHECKING:  # the ops module imports this one to apply an inverse
    from alkera_core.files.ops import Operations, OperationState

#: A move of a folder holding more than this many children is an Operation, not
#: a request: the statement that rewrites the subtree's paths would hold its
#: locks for longer than a request may. The moved folder itself is not one of
#: them, so a folder holding exactly this many children still moves inline.
MAX_INLINE_MOVE_NODES: Final = 10_000

#: The kinds that may hold children. A file is not a folder even on a mount that
#: would let you try.
CONTAINER_KINDS: Final = frozenset({"folder"})

#: What a caller may ask for when the name it wants is taken.
ConflictBehavior = Literal["fail", "rename"]

#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()


@dataclass(frozen=True, slots=True)
class NodeAttrs:
    """The POSIX attributes a create carries.

    Stored and round-tripped verbatim, never evaluated for access: ``mode`` says
    what a materialized copy should look like on a box, not who may read the
    node here.
    """

    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    size: int = 0
    atime_ns: int = 0
    mtime_ns: int = 0
    birthtime_ns: int = 0
    xattrs: dict[str, Any] = field(default_factory=dict)


def _symlink_kind_for(kind: str, requested: str | None) -> str | None:
    """The ``symlink_kind`` column for a node of ``kind``.

    Only a symlink carries one, so a caller that names a kind for a folder is
    confused about what it is creating and is told rather than having the value
    silently dropped. The check constraint would refuse an unknown word as an
    ``IntegrityError``; refusing it here makes it the caller's 400.
    """
    if kind != "symlink":
        if requested is not None:
            raise InvalidRequest(f"a {kind} has no symlink kind")
        return None
    chosen = requested if requested is not None else "relative"
    if chosen not in SYMLINK_KINDS:
        raise InvalidRequest(f"unknown symlink kind {chosen!r}")
    return chosen


#: What a symlink whose target leaves the drive is refused with.
LINK_OUTSIDE_CODE = "files.link_outside_tree"


def _refuse_an_escaping_link(kind: str, below: int, target: bytes | None) -> None:
    """Refuse a symlink whose target names a place outside the drive: a host
    path, or a ``..`` that climbs above the root (``below`` is how many folders
    the link sits under it). Every machine that materializes the drive would
    otherwise be handed a door out of the tree it put the drive in."""
    if target is None or not stored_link_stays_inside(kind, below, target):
        raise InvalidRequest(
            LINK_OUTSIDE_CODE,
            "a symlink must point inside the drive: an absolute path or a '..' "
            "above the drive's root is refused",
        )


def refuse_links_a_move_would_open(
    subtree: Sequence[FileNode], *, root_depth: int, new_root_depth: int
) -> None:
    """Refuse a move or copy that would leave a stored link pointing out of the
    drive: one of ``subtree``'s links (the moved node at ``root_depth`` and
    everything under it, landing with the node at ``new_root_depth``) whose
    target stays inside where it is and would climb above the root where it
    lands. A link that already points out (stored before the rule) is left as
    it is: the move does not make it any worse."""
    shift = new_root_depth - root_depth
    if shift >= 0:
        # Deeper (or level) leaves every relative climb at least as far inside.
        return
    for row in subtree:
        if row.kind != "symlink" or row.symlink_target is None:
            continue
        kind = row.symlink_kind or "relative"
        target = bytes(row.symlink_target)
        if stored_link_stays_inside(kind, row.depth - 1, target) and not stored_link_stays_inside(
            kind, row.depth - 1 + shift, target
        ):
            raise InvalidRequest(
                LINK_OUTSIDE_CODE,
                "a symlink in what you are moving would point outside the drive from its "
                "new place; move it somewhere deeper, or change the link first",
            )


#: The refusal a person's create or rename gets when a sibling is its name in
#: another Unicode normalization form. The same code as an exact clash, because
#: to a person it is one: the folder already shows that name.
NORMALIZATION_TWIN_MESSAGE = "that name is taken in this folder in another Unicode form"


def _free_or_refused(
    name: bytes,
    taken: set[bytes],
    *,
    conflict: ConflictBehavior,
    lease: LeaseContext | None,
) -> bytes:
    """The name to write: ``name``, a free variant of it, or a refusal.

    ``conflict="rename"`` steps past a name a sibling holds exactly or in
    another normalization form. Otherwise a normalization twin is refused here,
    and an exact clash is left to the unique index, which has no window. Twins
    are compared, never rewritten: the stored bytes are what the caller sent.

    The lease holder is the exception. It reports what its own disk holds, and
    a Linux disk can hold both forms; refusing one would leave that file on the
    machine and nowhere else. Its twin is flagged, like a case pair, instead.
    """
    keys = {names.normalization_key(sibling) for sibling in taken}
    if conflict == "rename" and (name in taken or names.normalization_key(name) in keys):
        return conflict_rename(
            name, lambda candidate: candidate in taken or names.normalization_key(candidate) in keys
        )
    holder = lease is not None and lease.epoch is not None
    if not holder and names.normalization_twin(taken, name) is not None:
        raise Conflict("files.exists", NORMALIZATION_TWIN_MESSAGE)
    return name


def _name_columns(name: bytes, *, macos_safe: bool) -> dict[str, Any]:
    """The derived name columns for one name — the only place they are spelled."""
    flags = names.flags(name)
    display = names.display(name)
    try:
        name.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "binary"
    return {
        "name": name,
        "name_display": display,
        "name_key": names.name_key(name),
        "name_encoding": encoding,
        "flags": json.dumps(
            {
                "windows_safe": flags.windows_safe,
                "macos_safe": macos_safe,
                "display_warning": flags.display_warning,
            }
        ),
    }


def assert_not_moving(node: FileNode) -> None:
    """Refuse a structural write into a subtree a batched move is rewriting.

    The one check every request path shares. A large move rewrites ``path_ids``
    for tens of thousands of rows over several transactions, so a write that
    landed in the middle would either be rewritten out from under its caller or
    leave a row whose path disagrees with its parent walk. Refusing is the only
    honest answer, and it is a conflict rather than a precondition failure
    because retrying it later succeeds.
    """
    if node.state == "moving":
        raise Conflict("files.moving", "a move of this subtree is in progress")


async def rederive_folding_flags(repo: FilesRepo, parent_id: uuid.UUID) -> None:
    """Recompute ``macos_safe`` for every live child of one folder.

    The flag is a property of a name *among its siblings*, not of the name
    alone, so it has to be re-derived whenever the sibling set changes. A
    rename, a move in or out, a trash and a restore can each dissolve a folding
    collision as easily as they can create one, and a flag that is only ever
    cleared would leave the survivor of a dissolved pair permanently marked
    unsafe for a twin that is no longer there.

    The caller must already hold the folder gate — the parent row ``FOR
    UPDATE``, taken in the fixed drive → parent → node order *before* it writes
    any sibling. This recompute writes the whole sibling set, so two callers
    that each wrote a row first and only then arrived here would each be
    holding the row the other still has to update: the detector picks a victim
    and an ordinary rename answers 40P01.

    The window function decides the whole sibling set in one pass — a row at a
    time cannot answer a question about a group. The read comes first and the
    single ``UPDATE`` only follows when some row's answer has actually changed,
    so the overwhelmingly common folder, where no two names fold together,
    costs a query and no write at all.
    """
    params = {"parent": parent_id, "org": repo.scope.org_team_id}
    changed = (
        await repo.session.execute(
            text(
                "SELECT 1 FROM (SELECT flags_names, "
                "count(*) OVER (PARTITION BY name_key) > 1 AS collides FROM file_nodes "
                "WHERE parent_id = :parent AND org_team_id = :org AND trashed_at IS NULL) AS s "
                "WHERE s.flags_names -> 'macos_safe' IS DISTINCT FROM to_jsonb(NOT s.collides) "
                "LIMIT 1"
            ),
            params,
        )
    ).first()
    if changed is None:
        return
    await repo.session.execute(
        text(
            "UPDATE file_nodes AS n SET flags_names = "
            "jsonb_set(n.flags_names, '{macos_safe}', to_jsonb(NOT s.collides)) "
            "FROM (SELECT id, flags_names, count(*) OVER (PARTITION BY name_key) > 1 AS collides "
            "FROM file_nodes WHERE parent_id = :parent AND org_team_id = :org "
            "AND trashed_at IS NULL) AS s "
            "WHERE n.id = s.id AND n.org_team_id = :org "
            "AND n.flags_names -> 'macos_safe' IS DISTINCT FROM to_jsonb(NOT s.collides)"
        ),
        params,
    )


def _as_conflict(exc: IntegrityError) -> Exception:
    """A unique violation is the taken name; anything else is not ours to rename.

    Mapping every ``IntegrityError`` to ``files.exists`` would report a missing
    parent or a broken CHECK as a name collision, which is the kind of lie that
    sends a caller into an infinite conflict-rename loop.
    """
    if is_unique_violation(exc):
        return Conflict("files.exists", "that name is taken in this folder")
    return exc


def _validated(name: bytes) -> None:
    """Refuse a name Linux itself would refuse, in the Files vocabulary.

    :func:`names.validate` raises a plain ``ValueError``, which no handler maps
    — a create or a rename carrying a ``/`` or a 300-byte name reached the
    platform's catch-all as an opaque 500, so a person was told to retry
    something that can never succeed. The code carries which rule refused it so
    a client can name the rule instead.
    """
    try:
        names.validate(name)
    except names.InvalidName as exc:
        raise InvalidRequest(f"files.invalid_name.{exc.code}", str(exc)) from exc


class Namespace:
    """Every structural change to one tenant's tree."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        checkpoints: Checkpoints = NO_CHECKPOINTS,
        ino: InoAllocator | None = None,
        ceilings: CeilingsResolver | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._ino = ino if ino is not None else InoAllocator(repo)
        self._ceilings = ceilings

    # ---- create ----------------------------------------------------------

    async def create(
        self,
        drive_id: DriveId,
        parent_id: NodeId,
        kind: str,
        name: bytes,
        *,
        attrs: NodeAttrs | None = None,
        conflict: ConflictBehavior = "fail",
        symlink_target: bytes | None = None,
        symlink_kind: str | None = None,
        subtype: str | None = None,
        artifact: bool = False,
        lease: LeaseContext | None = None,
    ) -> FileNode:
        """Add one node under ``parent_id``.

        A new node inherits the restricting half of its parent's ``flags``. The
        decider reads only a node's OWN bits — never its chain — so a subtree
        restriction has to be stamped as each node is created; without it a
        scratch file inside a NO_DOWNLOAD chat folder was downloadable, which is
        the whole restriction defeated one level down.

        ``artifact`` is the exception: a deliverable the run is meant to hand
        back (what lands under a chat's ``outputs/`` or a replication context's
        ``last_output/``) stays downloadable, and marks its subtree so that
        everything written beneath it does too.

        The duplicate-name refusal is the partial unique index on
        ``(parent_id, name) WHERE trashed_at IS NULL``, not a pre-check: a
        pre-check would have a window in which a second writer takes the name,
        and the index has none. ``conflict="rename"`` still reads the siblings,
        but only to pick a candidate — if it loses the race for that one the
        index refuses it too.

        ``symlink_kind`` says how the stored target is read back (``relative``
        against this directory, ``canonical`` against the org namespace,
        ``host`` verbatim on some machine). A symlink created without one is
        ``relative``: it is the only kind whose text means the same thing on
        every machine, so an unclassified link never becomes an absolute path
        into a stranger's disk.

        A create is a write *inside* the parent, so it is fenced against the
        parent's covering lease exactly as a rename or a move is: putting a new
        node into somebody else's mount is their write to make.
        """
        _validated(name)
        link_kind = _symlink_kind_for(kind, symlink_kind)
        attributes = attrs if attrs is not None else NodeAttrs()
        drive, parent, _ = await self._repo.lock_chain(drive_id, parent_id, None)
        if drive is None:
            raise NotFound(f"no drive {drive_id}")
        if parent is None:
            raise NotFound(f"no node {parent_id}")
        if parent.kind not in CONTAINER_KINDS:
            raise InvalidRequest(f"a {parent.kind} may not hold children")
        assert_traversal_rules(parent)
        if parent.drive_id != drive_id:
            raise InvalidRequest("the parent is not in that drive")
        assert_not_moving(parent)
        # After the policy decision, before the write: a create inside somebody
        # else's mount is theirs to make, not ours.
        covering = await fenced_write_for(self._repo, parent, lease, into=True)
        # A node is new usage. Once the org or the caller is at a ceiling nothing
        # new lands — except the push that goes with the holder's release, whose
        # folders exist on the box alone. A lease on its own buys nothing: the
        # live plane writes under the same fence continuously, and exempting
        # that would let one awake box mint nodes past the ceiling forever.
        if not is_final_push(covering, lease):
            await QuotaService(
                self._repo, self._ctx, self._clock, self._store, ceilings=self._ceilings
            ).assert_room(drive_id, bytes=0, nodes=1, parent_path=parent.path_ids)

        if link_kind is not None:
            _refuse_an_escaping_link(link_kind, parent.depth, symlink_target)

        siblings = await self._repo.siblings(parent_id)
        taken = {row.name for row in siblings}
        name = _free_or_refused(name, taken, conflict=conflict, lease=lease)

        colliding = self._folding_collisions(siblings, name)
        node_id = uuid.uuid4()
        columns = _name_columns(name, macos_safe=not colliding)
        ino = await self._ino.allocate(drive_id)
        params: dict[str, Any] = {
            "id": node_id,
            "ino": ino,
            "drive_id": drive_id,
            "org": self._repo.scope.org_team_id,
            "parent_id": parent_id,
            "kind": kind,
            "subtype": subtype,
            "path_ids": f"{parent.path_ids}.{ino_label(ino)}",
            "depth": parent.depth + 1,
            "symlink_target": symlink_target,
            "symlink_kind": link_kind,
            "mode": attributes.mode,
            "uid": attributes.uid,
            "gid": attributes.gid,
            "size": attributes.size,
            "atime": attributes.atime_ns,
            "mtime": attributes.mtime_ns,
            "birthtime": attributes.birthtime_ns,
            "xattrs": json.dumps(stored_xattrs(attributes.xattrs)),
            "node_flags": flags_for_child(
                parent.flags, artifact=artifact, parent_subtype=parent.subtype, name=name
            ),
            "created_by": await history.owner_ref(self._repo, self._ctx, drive_id, parent.path_ids),
            **columns,
        }
        statement = text(
            "INSERT INTO file_nodes ("
            "id, ino, drive_id, org_team_id, parent_id, kind, subtype, name, name_display, "
            "name_key, name_encoding, flags_names, path_ids, depth, symlink_target, symlink_kind, "
            "mode, uid, "
            "gid, nlink, rdev, size, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, "
            "flags, state, trust, traversal_only, metadata, created_by) VALUES ("
            ":id, :ino, :drive_id, :org, :parent_id, :kind, :subtype, :name, :name_display, "
            ":name_key, :name_encoding, CAST(:flags AS jsonb), CAST(:path_ids AS ltree), :depth, "
            ":symlink_target, :symlink_kind, :mode, :uid, :gid, 1, 0, :size, :atime, :mtime, "
            "(EXTRACT(EPOCH FROM now()) * 1000000000)::bigint, "
            ":birthtime, CAST(:xattrs AS jsonb), 1, :node_flags, 'live', 'own', false, "
            "'{}'::jsonb, :created_by) "
            "RETURNING id"
        )
        await self._insert_or_conflict(statement, params)

        if colliding:
            await self._mark_folding_unsafe([row.id for row in colliding])
        made = await self._reload(NodeId(node_id))
        await self._announce(
            made,
            kind_of_change="create",
            before=None,
            after={"parent_id": str(parent_id), "kind": kind, "ino": ino},
        )
        # A created node is headless: it has no version, so a recount over
        # `file_versions` counts neither its bytes nor it as a file. Charging
        # the parent for either here would put the aggregate permanently ahead
        # of the truth, because the head swap that lands the bytes is what
        # appends them. Only the folder's own entry count moves.
        await stats.add_delta(
            self._repo,
            node_id=NodeId(parent_id),
            bytes_delta=0,
            files_delta=0,
            direct_children_delta=1,
            child_change_at=self._clock.now(),
        )
        if admitted_inbound(covering) is not None:
            # The node exists on the drive and not on the machine holding the
            # chat: record it so the holder pulls it down on its next drain.
            await LiveEntriesService(self._repo, self._ctx).accept_inbound(made, kind="inbound")
        return made

    # ---- rename ----------------------------------------------------------

    async def rename(
        self,
        node_id: NodeId,
        new_name: bytes,
        *,
        if_match: int,
        conflict: ConflictBehavior = "fail",
        lease: LeaseContext | None = None,
        op: Operations | None = None,
    ) -> FileNode:
        """Give one node a new name in its own folder.

        One UPDATE carries both the identity and the precondition, so a caller
        holding a stale ``etag`` changes nothing at all: zero rows updated, a
        412, and every other row — the one that already holds the name included
        — exactly as it was.
        """
        _validated(new_name)
        node = await self._require(node_id)
        assert_not_moving(node)
        if home_owner(node) is not None:
            # A home's stored name is its owner's id, which is how it is found
            # again; what a person sees is the owner's name, so there is
            # nothing here a rename could change for them.
            raise InvalidRequest("a home folder cannot be renamed")
        # The folder gate, taken before the first sibling row is written. A
        # rename rewrites its own row and then re-derives the folding flag of
        # every sibling, so two renames in one folder each end up holding the
        # row the other still has to update: without a lock they take *first*,
        # in the one fixed order, the detector picks a victim and an ordinary
        # rename answers 40P01. Locking after the node UPDATE would not do —
        # the losing side already holds a sibling by then.
        #
        # No drive row: nothing writes a folder's children without the folder
        # or, under a lease, without the leased folder above it, which
        # ``lock_node`` takes first -- the gate the holder's tree report holds
        # for its batch. So a rename queues only behind a writer in its own
        # folder or its own lease, never behind the org's other traffic. The
        # locks come before the fence because the lease row sits after every
        # node in the fixed order.
        if node.parent_id is not None:
            await self._repo.lock_node(NodeId(node.parent_id))
        # After the policy decision, before the write: a rename inside somebody
        # else's mount is theirs to make, not ours.
        covering = await fenced_write_for(self._repo, node, lease)
        renamed_inbound = admitted_inbound(covering)
        if node.parent_id is None:
            raise InvalidRequest("the drive root has no name to change")
        # A plain read, only for a caller whose etag is current: a stale caller
        # is told about its own staleness first, by the UPDATE below, which is
        # the order the contract states — a taken name is not news to a client
        # that has to re-read anyway. The unique index still decides a name
        # taken between this read and the write.
        if conflict == "fail" and node.etag == if_match and await self._name_taken(node, new_name):
            raise Conflict("files.exists", "that name is taken in this folder")

        siblings = [
            row for row in await self._repo.siblings(NodeId(node.parent_id)) if row.id != node.id
        ]
        new_name = _free_or_refused(
            new_name, {row.name for row in siblings}, conflict=conflict, lease=lease
        )
        colliding = self._folding_collisions(siblings, new_name)
        columns = _name_columns(new_name, macos_safe=not colliding)
        updated = await self._update_or_conflict(
            text(
                "UPDATE file_nodes SET name = :name, name_display = :name_display, "
                "name_key = :name_key, name_encoding = :name_encoding, "
                "flags_names = CAST(:flags AS jsonb), etag = etag + 1, updated_at = now() "
                "WHERE id = :id AND org_team_id = :org AND etag = :if_match "
                "AND trashed_at IS NULL RETURNING id"
            ),
            {
                "id": node_id,
                "org": self._repo.scope.org_team_id,
                "if_match": if_match,
                **columns,
            },
        )
        if updated is None:
            raise PreconditionFailed(f"node {node_id} is not at etag {if_match}")
        await rederive_folding_flags(self._repo, node.parent_id)
        if op is not None:
            await self._record_inverse(op, _rename_inverse(node_id, node.name))
        renamed = await self._reload(node_id)
        await self._announce(
            renamed,
            kind_of_change="rename",
            before={"name_key": node.name_key},
            after={"name_key": renamed.name_key},
        )
        if renamed_inbound is not None:
            # The name changed on the drive and not on the machine: the holder
            # moves its own copy rather than re-downloading the same bytes.
            await LiveEntriesService(self._repo, self._ctx).accept_inbound(
                renamed, kind="inbound_rename"
            )
        return renamed

    # ---- move ------------------------------------------------------------

    async def move(
        self,
        node_id: NodeId,
        new_parent_id: NodeId,
        *,
        if_match: int,
        conflict: ConflictBehavior = "fail",
        lease: LeaseContext | None = None,
        op: Operations | None = None,
    ) -> FileNode | OperationState:
        """Re-parent one node and rewrite its subtree's paths.

        A folder holding more than :data:`MAX_INLINE_MOVE_NODES` children is
        not a request: it becomes a queued ``move`` operation and this returns that
        operation's state instead of a node, because the tree has not changed
        yet and returning the unmoved node would say it had.

        The cycle guard is ``NOT (p.path_ids <@ n.path_ids)`` inside the same
        UPDATE, with ``p`` joined rather than read into Python first: under READ
        COMMITTED the statement re-reads a row it had to wait for, so a move
        that was legal when its caller looked is refused if the other move made
        it a cycle in between. The subtree rewrite that follows keys on the path
        the node has under the namespace lock, because an ancestor's move can have
        re-rooted it since the caller's read without changing its etag. A move
        is never a delete plus an add — the node keeps its id, its ino and its
        history.
        """
        node = await self._require(node_id)
        assert_not_moving(node)
        # The namespace, exclusively, before anything else this move does: the
        # subtree rewrite below re-roots paths a create under it would derive
        # from, and the drop target may create a folder (a shared hold this
        # transaction must not then try to raise). Everything the move read
        # before this point may be stale once it is granted.
        await self._checkpoints.reach("namespace.before_move_lock")
        await self._repo.lock_namespace(DriveId(node.drive_id), exclusive=True)
        new_parent_id = await self._drop_target(new_parent_id)
        read_path = node.path_ids
        # The prefix query returns the node itself alongside its descendants;
        # the cap is on the children the move has to rewrite, not on the folder
        # the caller named, so the node is subtracted back out.
        subtree = await self._repo.nodes_by_path_prefix(read_path)
        children = len(subtree) - 1
        if children > MAX_INLINE_MOVE_NODES:
            # The queued plan re-parents later and never passes the check the
            # inline move makes under its locks, so a chat is refused here.
            from alkera_core.files.objects_bridge import assert_chat_stays_in_workspace

            destination = await self._repo.node(new_parent_id)
            if destination is not None:
                await assert_chat_stays_in_workspace(self._repo, node, destination)
            # The queued plan takes no drive or node lock, so the source fence
            # is the only lock this branch holds and may come first.
            await fenced_write_for(self._repo, node, lease)
            return await self._route_large_move(node, new_parent_id, if_match=if_match, lease=lease)

        # The folder gate, over *both* folders this move rewrites. A move
        # re-derives the folding flags of the source folder and of the
        # destination, so a gate that took only the destination would let two
        # opposite cross-folder moves each end up holding the folder the other
        # still has to write. The namespace (held above), then the two parents
        # by ascending id, then the node: one total order every writer of this
        # pair agrees on, so there is no pair of folders left to take in
        # opposite orders.
        folders = {new_parent_id}
        if node.parent_id is not None:
            folders.add(NodeId(node.parent_id))
        for folder_id in sorted(folders):
            await self._repo.lock_node(folder_id)
        locked = await self._repo.lock_node(node_id)
        if locked is None:
            raise NotFound(f"no node {node_id}")
        node = locked
        assert_not_moving(node)
        # The path the subtree rewrite keys on is the one the node has under the
        # lock, not the one the read above saw. An ancestor's move that committed
        # in between rewrote this node's path without touching its etag, so the
        # etag check below lets this move through — and a rewrite keyed on the
        # stale prefix would miss every descendant, leaving each with a path no
        # ancestor carries, which a later move into one of them walks past the
        # cycle guard on.
        old_path = node.path_ids
        if old_path != read_path:
            subtree = await self._repo.nodes_by_path_prefix(old_path)
        # The source fence comes after the chain lock, not before it: the lease
        # row is locked at update strength and sits after the namespace and the
        # nodes in the fixed order, and an upload opening under the same lease
        # holds the namespace while it asks for the lease.
        await fenced_write_for(self._repo, node, lease)
        parent = await self._repo.node(new_parent_id)
        if parent is None:
            raise NotFound(f"no node {new_parent_id}")
        if parent.kind not in CONTAINER_KINDS:
            raise InvalidRequest(f"a {parent.kind} may not hold children")
        assert_traversal_rules(parent)
        # Deferred: ``objects_bridge`` builds on this module.
        from alkera_core.files.objects_bridge import assert_chat_stays_in_workspace

        await assert_chat_stays_in_workspace(self._repo, node, parent)
        refuse_links_a_move_would_open(
            subtree, root_depth=node.depth, new_root_depth=parent.depth + 1
        )
        # The re-parent statement rewrites ``parent_id`` and ``path_ids`` but
        # never ``drive_id``, and the chain lock above took only the source
        # drive: a move across two drives of one org would commit a node whose
        # drive names one tree while its path sits in the other.
        if parent.drive_id != node.drive_id:
            raise InvalidRequest("the destination is not in that drive")
        # The source was fenced above; the destination subtree has a holder of
        # its own, and grafting a tree into somebody else's mount is their
        # write to make.
        covering = await fenced_write_for(self._repo, parent, lease, into=True)
        # Bytes the caller owns that enter a folder they are limited in count
        # there; a move within one scope, or out of one, moves nothing that
        # binds. Only the push that goes with the holder's release is spared —
        # holding a lease is not itself a reason to carry bytes past a limit.
        if not is_final_push(covering, lease):
            subject = subject_ref(self._ctx)
            own_bytes = sum(
                row.size
                for row in subtree
                if row.kind == "file" and row.trashed_at is None and row.created_by == subject
            )
            await QuotaService(
                self._repo, self._ctx, self._clock, self._store, ceilings=self._ceilings
            ).assert_scope_entry(
                DriveId(node.drive_id),
                bytes=own_bytes,
                source_path=old_path.rsplit(".", 1)[0] if "." in old_path else None,
                dest_path=parent.path_ids,
            )

        if conflict == "rename":
            # The node is its own sibling when the destination is the parent it
            # already has: without the exclusion a same-parent move renames the
            # node against itself and every such move is a rename.
            #
            # The rename below is a real write in the folder the node is still
            # standing in — the re-parent cannot carry the new name, because the
            # index would refuse the old one at the destination first — so a
            # candidate free only at the destination can still violate the live
            # sibling index at the source. The union is what makes the
            # intermediate state legal; the extra names only ever push the
            # conflict index up, never make one unavailable at the destination.
            source: Sequence[FileNode] = (
                await self._repo.siblings(NodeId(node.parent_id))
                if node.parent_id is not None
                else ()
            )
            taken = {
                row.name
                for row in (*await self._repo.siblings(new_parent_id), *source)
                if row.id != node_id
            }
            if node.name in taken:
                await self.rename(
                    node_id,
                    conflict_rename(node.name, taken.__contains__),
                    if_match=if_match,
                    conflict="fail",
                )
                if_match += 1
                node = await self._require(node_id)

        await self._checkpoints.reach("namespace.before_move_update")
        moved = await self._update_or_conflict(
            text(
                "UPDATE file_nodes AS n SET parent_id = p.id, "  # noqa: S608
                "path_ids = p.path_ids || CAST(:label AS ltree), "
                "depth = nlevel(p.path_ids), etag = n.etag + 1, updated_at = now() "
                "FROM file_nodes AS p "
                "WHERE n.id = :id AND n.org_team_id = :org AND n.etag = :if_match "
                "AND n.trashed_at IS NULL AND p.id = :parent AND p.org_team_id = :org "
                "AND p.trashed_at IS NULL "
                f"AND NOT {subtree_sql('p.path_ids', 'n.path_ids')} "
                "RETURNING n.path_ids::text, n.depth"
            ),
            {
                "id": node_id,
                "org": self._repo.scope.org_team_id,
                "if_match": if_match,
                "parent": new_parent_id,
                "label": ino_label(node.ino),
                **SUBTREE_DEPTH_BIND,
            },
            returns="row",
        )
        await self._checkpoints.reach("namespace.after_move_update")
        if moved is None:
            raise await self._why_move_failed(node_id, new_parent_id, if_match)

        new_path, new_depth = str(moved[0]), int(moved[1])
        await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET "  # noqa: S608
                "path_ids = CAST(:new_path AS ltree) || subpath(path_ids, :old_len), "
                "depth = depth + :delta, updated_at = now() "
                "WHERE org_team_id = :org "
                f"AND {subtree_sql('path_ids', 'CAST(:old_path AS ltree)')} "
                "AND id <> :id"
            ),
            {
                "new_path": new_path,
                "old_path": old_path,
                "old_len": old_path.count(".") + 1,
                "delta": new_depth - node.depth,
                "org": self._repo.scope.org_team_id,
                "id": node_id,
                **SUBTREE_DEPTH_BIND,
            },
        )
        if node.parent_id is not None:
            await rederive_folding_flags(self._repo, node.parent_id)
        await rederive_folding_flags(self._repo, new_parent_id)
        if op is not None and node.parent_id is not None:
            await self._record_inverse(
                op, _move_inverse(node_id, NodeId(node.parent_id), new_parent_id)
            )
        # The subtree hangs under different ancestors now, so every cached ACL
        # in it — the moved node's own included — describes a chain that no
        # longer exists. Without this the node keeps the interned body of the
        # parent it has LEFT: moving something out of a shared folder would not
        # take the sharing away, and moving something into one would not hand it
        # over. Direct grants are untouched — they belong to the node and travel
        # with it.
        #
        # Marking, not re-deriving: a marked node reads through to the ancestor
        # chain, which is the truth, so the window before the queued repair lands
        # is slower and never wrong. Re-deriving here would additionally bump the
        # node's version a second time — a move moves it once — and emit a second
        # event for one change. The batched move marks the same way, so both
        # sizes of move behave identically.
        await acl.invalidate_subtree(self._repo, self._ctx, await self._reload(node_id))
        result = await self._reload(node_id)
        await self._announce(
            result,
            kind_of_change="move",
            before={"parent_id": str(node.parent_id), "depth": node.depth},
            after={"parent_id": str(new_parent_id), "depth": new_depth},
        )
        return result

    async def _drop_target(self, new_parent_id: NodeId) -> NodeId:
        """The folder a write aimed at ``new_parent_id`` really lands in.

        Every destination but a chat is itself. A chat is a real folder, so
        without this a drop onto a conversation would sit at the chat's top
        level, beside the working folders and in the one place nothing reads.
        It is resolved before the destination is locked, counted or checked, so
        the lock order, the queued plan and everything downstream all name the
        folder that is actually written — never the chat the caller typed.
        """
        # Imported here: the bridge builds a Namespace of its own to mint a
        # chat's folders, so naming it at module scope would be a cycle.
        from alkera_core.files.objects_bridge import drop_target_for

        parent = await self._repo.node(new_parent_id)
        if parent is None:
            raise NotFound(f"no node {new_parent_id}")
        landing = await drop_target_for(self._repo, self._ctx, self, parent)
        return NodeId(landing.id)

    async def _route_large_move(
        self,
        node: FileNode,
        new_parent_id: NodeId,
        *,
        if_match: int,
        lease: LeaseContext | None = None,
    ) -> OperationState:
        """Turn an oversized move into a queued operation and hand it back.

        Nothing is stamped ``moving`` here: the request's job is to record what
        was asked for, and the runner is what owns the subtree from the moment
        it marks it. Marking in the request would leave the tree frozen behind
        a caller that never came back.

        The destination is checked here rather than left to the runner: the
        runner's re-parent is the same statement the inline path runs, which
        rewrites the path and not the drive, so queueing a cross-drive move
        would only defer the corruption.
        """
        parent = await self._repo.node(new_parent_id)
        if parent is None:
            raise NotFound(f"no node {new_parent_id}")
        if parent.kind not in CONTAINER_KINDS:
            raise InvalidRequest(f"a {parent.kind} may not hold children")
        assert_traversal_rules(parent)
        if parent.drive_id != node.drive_id:
            raise InvalidRequest("the destination is not in that drive")
        await fenced_write_for(self._repo, parent, lease, into=True)
        moving = await self._repo.nodes_by_path_prefix(node.path_ids)
        refuse_links_a_move_would_open(
            moving, root_depth=node.depth, new_root_depth=parent.depth + 1
        )

        from alkera_core.files.large_move import plan_body
        from alkera_core.files.ops import Operations

        ops = Operations(self._repo, self._ctx, self._clock, self._store)
        total = len(moving)
        started = await ops.start("move", drive_id=DriveId(node.drive_id), total=total)
        self._repo._require_open()
        await self._repo.session.execute(
            text(
                "UPDATE file_ops SET result = CAST(:body AS jsonb), result_node_id = :node "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "id": started.id,
                "org": self._repo.scope.org_team_id,
                "node": node.id,
                "body": json.dumps(
                    plan_body(
                        NodeId(node.id),
                        new_parent_id,
                        if_match=if_match,
                        old_path=node.path_ids,
                    )
                ),
            },
        )
        return await ops.get(started.id)

    async def _why_move_failed(
        self, node_id: NodeId, new_parent_id: NodeId, if_match: int
    ) -> Exception:
        """Name the reason a zero-row move gives, by re-reading the two rows."""
        node = await self._repo.node(node_id)
        if node is None:
            return NotFound(f"no node {node_id}")
        parent = await self._repo.node(new_parent_id)
        if parent is None:
            return NotFound(f"no node {new_parent_id}")
        if node.etag != if_match:
            return PreconditionFailed(f"node {node_id} is not at etag {if_match}")
        return Conflict("files.cycle", "a node may not be moved inside its own subtree")

    # ---- reads -----------------------------------------------------------

    async def resolve_path(
        self,
        drive_id: DriveId,
        path: str,
        *,
        drive: FileDrive | None = None,
        below: NodeId | None = None,
    ) -> FileNode | None:
        """The node one posix path names, or ``None``.

        Three statements whatever the depth: the drive, its root, and one
        batched read of every node in the drive whose name is one of the path's
        segments. The walk itself is then a dict lookup per segment, so a
        256-deep path costs the same round trips as a 2-deep one.

        ``drive`` is a parameter so a request that has already resolved the
        drive it is addressing pays two: the row is the same row, and reading
        it a second time inside the same transaction can only return what the
        caller is holding. A handle for a *different* drive is ignored, so the
        id in the path is still what decides which drive is walked.

        ``below`` starts the walk at that node instead of the drive root, so
        ``path`` is the step DOWN from a folder the caller holds. A holder is
        told a folder's path only from the deepest ancestor it may read — a box
        on a chat's lease reads nothing above the chat folder and is told the
        folder's bare name — so an absolute path is not something every caller
        can spell, and what is under a node it holds is addressed from the
        node. A node of another drive starts nothing; an empty step is the
        node itself.
        """
        if drive is None or drive.id != drive_id:
            drive = await self._repo.drive(drive_id)
        if drive is None or drive.root_node_id is None:
            return None
        start = await self._repo.node(below if below is not None else NodeId(drive.root_node_id))
        if start is not None and start.drive_id != drive.id:
            start = None
        segments = [decode_from_surface(part, "posix") for part in path.split("/") if part]
        if start is None or not segments:
            return start
        rows = await self._repo.nodes_named(drive_id, segments)
        by_parent: dict[tuple[uuid.UUID, bytes], FileNode] = {
            (row.parent_id, row.name): row for row in rows if row.parent_id is not None
        }
        walked = start
        for segment in segments:
            found = by_parent.get((walked.id, segment))
            if found is None:
                return None
            walked = found
        return walked

    async def subtree(self, node_id: NodeId) -> AsyncIterator[FileNode]:
        """Every node at or under ``node_id``, the node itself first."""
        node = await self._require(node_id)
        for row in await self._repo.nodes_by_path_prefix(node.path_ids):
            yield row

    # ---- internals -------------------------------------------------------

    def _folding_collisions(self, siblings: Sequence[FileNode], name: bytes) -> list[FileNode]:
        """The live siblings a case- or normalization-folding client would confuse."""
        key = names.name_key(name)
        return [row for row in siblings if row.name != name and names.name_key(row.name) == key]

    async def _mark_folding_unsafe(self, ids: Sequence[uuid.UUID]) -> None:
        """Flip ``macos_safe`` off on the siblings the new name now collides with."""
        await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET flags_names = flags_names || '{\"macos_safe\": false}' "
                "WHERE id = ANY(:ids) AND org_team_id = :org"
            ),
            {"ids": list(ids), "org": self._repo.scope.org_team_id},
        )

    async def _record_inverse(self, op: Operations, inverse: Any) -> None:
        """Hand the operation its inverse, inside this mutation's transaction."""
        await op.record_inverse(inverse)

    async def _require(self, node_id: NodeId) -> FileNode:
        node = await self._repo.node(node_id)
        if node is None:
            raise NotFound(f"no node {node_id}")
        return node

    async def _reload(self, node_id: NodeId) -> FileNode:
        """Read a node the raw statements above have just changed.

        A ``SELECT`` alone would hand back the identity-mapped instance with the
        values it was loaded with — the columns a ``text()`` UPDATE changed are
        invisible to the mapper — so a caller would be told its rename did not
        happen. ``refresh`` re-reads the row into that instance.
        """
        node = await self._require(node_id)
        await self._repo.session.refresh(node)
        return node

    async def _insert_or_conflict(self, statement: Any, params: dict[str, Any]) -> None:
        """Run an INSERT inside a SAVEPOINT, turning the unique index into a 409.

        The savepoint is what lets the caller's transaction survive the
        violation: without it the failed statement would poison the whole
        transaction and the 409 could not be raised from inside it.
        """
        self._repo._require_open()
        savepoint = await self._repo.session.begin_nested()
        try:
            await self._repo.session.execute(statement, params)
        except IntegrityError as exc:
            await savepoint.rollback()
            raise _as_conflict(exc) from exc
        await savepoint.commit()

    async def _name_taken(self, node: FileNode, name: bytes) -> bool:
        """Whether a live sibling other than ``node`` already has ``name``.

        A plain read, no lock: the unique index still decides under the folder
        gate, so a name taken between this look and the write is the same
        conflict, answered from the index instead.
        """
        self._repo._require_open()
        found = await self._repo.session.execute(
            text(
                "SELECT 1 FROM file_nodes WHERE parent_id = :parent AND name = :name "
                "AND id <> :id AND org_team_id = :org AND trashed_at IS NULL LIMIT 1"
            ),
            {
                "parent": node.parent_id,
                "name": name,
                "id": node.id,
                "org": self._repo.scope.org_team_id,
            },
        )
        return found.first() is not None

    async def _update_or_conflict(
        self, statement: Any, params: dict[str, Any], *, returns: str = "id"
    ) -> Any:
        self._repo._require_open()
        savepoint = await self._repo.session.begin_nested()
        try:
            result = await self._repo.session.execute(statement, params)
            row = result.first()
        except IntegrityError as exc:
            await savepoint.rollback()
            raise _as_conflict(exc) from exc
        await savepoint.commit()
        if row is None:
            return None
        return row if returns == "row" else row[0]

    async def _announce(
        self,
        node: FileNode,
        *,
        kind_of_change: history.HistoryKind,
        before: history.HistorySnapshot | None,
        after: history.HistorySnapshot | None,
    ) -> None:
        """The history row and the outbox row, in the caller's transaction."""
        await history.record(
            self._repo,
            self._ctx,
            node_id=NodeId(node.id),
            kind=kind_of_change,
            before=before,
            after=after,
        )
        await history.emit_node_changed(
            self._repo,
            self._ctx,
            node_id=NodeId(node.id),
            drive_id=DriveId(node.drive_id),
            version=node.etag,
        )


#: What a caller asking for a file with no bytes is told: the bytes are what
#: makes a file, so the answer names the next call rather than the mistake.
FILE_NEEDS_CONTENT: Final = "files.file_needs_content"


@dataclass(frozen=True, slots=True)
class HolderNode:
    """One row a holder's report mints: a folder, or a file with no bytes yet.

    Everything a create derives is already decided by the caller -- the id, the
    ino, the path under a parent that may itself be minted in the same batch,
    the flags the parent passes on -- so a whole report lands in one INSERT.
    """

    id: uuid.UUID
    ino: int
    parent_id: uuid.UUID
    kind: Literal["file", "folder"]
    name: bytes
    path_ids: str
    depth: int
    flags: int
    mode: int
    #: The row's own modified time: the disk's for a file, now for a folder.
    mtime_ns: int | None = None
    holder_size: int | None = None
    holder_mtime_ns: int | None = None
    holder_hash: bytes | None = None


_INSERT_HOLDER_NODES: Final = """
INSERT INTO file_nodes (
    id, ino, drive_id, org_team_id, parent_id, kind, name, name_display, name_key,
    name_encoding, flags_names, path_ids, depth, mode, uid, gid, nlink, rdev, size,
    atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, state, trust,
    traversal_only, metadata, created_by, holder_size, holder_mtime_ns, holder_hash,
    holder_seq
)
SELECT t.id, t.ino, :drive, :org, t.parent_id, t.kind, t.name, t.display, t.key,
       t.encoding, CAST(t.name_flags AS jsonb), CAST(t.path AS ltree), t.depth, t.mode,
       0, 0, 1, 0, 0, CAST(:now_ns AS bigint), coalesce(t.mtime, CAST(:now_ns AS bigint)),
       CAST(:now_ns AS bigint), CAST(:now_ns AS bigint),
       '{}'::jsonb, 1, t.flags, 'live', 'own', false, '{}'::jsonb, t.created_by,
       t.hsize, t.hmtime, t.hhash,
       CASE WHEN t.kind = 'file' THEN CAST(:seq AS bigint) ELSE NULL END
FROM unnest(
    CAST(:ids AS uuid[]), CAST(:inos AS bigint[]), CAST(:parents AS uuid[]),
    CAST(:kinds AS text[]), CAST(:names AS bytea[]), CAST(:displays AS text[]),
    CAST(:keys AS text[]), CAST(:encodings AS text[]), CAST(:name_flags AS text[]),
    CAST(:paths AS text[]), CAST(:depths AS integer[]), CAST(:modes AS integer[]),
    CAST(:flags AS integer[]), CAST(:mtimes AS bigint[]), CAST(:hsizes AS bigint[]),
    CAST(:hmtimes AS bigint[]), CAST(:hhashes AS bytea[]), CAST(:created_bys AS uuid[])
) AS t(id, ino, parent_id, kind, name, display, key, encoding, name_flags, path, depth,
       mode, flags, mtime, hsize, hmtime, hhash, created_by)
"""


async def insert_holder_nodes(
    repo: FilesRepo,
    ctx: ActingContext,
    drive_id: DriveId,
    nodes: Sequence[HolderNode],
    *,
    holder_seq: int,
    now: Any,
) -> None:
    """Insert every row of a holder's report in ONE statement.

    Only the fenced holder may call this: it writes files with no head version
    and trusts the caller for every derived column. Its caller is the tree
    report (:mod:`alkera_core.files.lease_tree`), which fences before it gets
    here.
    Parents and children may share the statement: a foreign key is checked at
    its end, so the order of the rows does not matter.

    Rows are born ``macos_safe``; the caller re-derives the folding flag for
    every folder it wrote into once all of them exist.
    """
    if not nodes:
        return
    now_ns = int(now.timestamp() * 1_000_000_000)
    columns = [_name_columns(node.name, macos_safe=True) for node in nodes]
    owners = await history.owner_refs(repo, ctx, drive_id, [node.path_ids for node in nodes])
    savepoint = await repo.session.begin_nested()
    try:
        await repo.session.execute(
            text(_INSERT_HOLDER_NODES),
            {
                "drive": drive_id,
                "org": repo.scope.org_team_id,
                "now_ns": now_ns,
                "created_bys": owners,
                "seq": holder_seq,
                "ids": [node.id for node in nodes],
                "inos": [node.ino for node in nodes],
                "parents": [node.parent_id for node in nodes],
                "kinds": [node.kind for node in nodes],
                "names": [node.name for node in nodes],
                "displays": [column["name_display"] for column in columns],
                "keys": [column["name_key"] for column in columns],
                "encodings": [column["name_encoding"] for column in columns],
                "name_flags": [column["flags"] for column in columns],
                "paths": [node.path_ids for node in nodes],
                "depths": [node.depth for node in nodes],
                "modes": [node.mode for node in nodes],
                "flags": [node.flags for node in nodes],
                "mtimes": [node.mtime_ns for node in nodes],
                "hsizes": [node.holder_size for node in nodes],
                "hmtimes": [node.holder_mtime_ns for node in nodes],
                "hhashes": [node.holder_hash for node in nodes],
            },
        )
    except IntegrityError as exc:
        await savepoint.rollback()
        raise _as_conflict(exc) from exc
    await savepoint.commit()
    await history.record_creates(
        repo,
        ctx,
        [
            (
                NodeId(node.id),
                {"parent_id": str(node.parent_id), "kind": node.kind, "ino": node.ino},
            )
            for node in nodes
        ],
    )
    await stats.add_deltas(
        repo,
        {parent: count for parent, count in _children_by_parent(nodes).items()},
        child_change_at=now,
    )
    await rederive_folding_flags_many(repo, sorted(_children_by_parent(nodes)))


def _children_by_parent(nodes: Sequence[HolderNode]) -> dict[uuid.UUID, int]:
    counted: dict[uuid.UUID, int] = {}
    for node in nodes:
        counted[node.parent_id] = counted.get(node.parent_id, 0) + 1
    return counted


async def rederive_folding_flags_many(repo: FilesRepo, parent_ids: Sequence[uuid.UUID]) -> None:
    """:func:`rederive_folding_flags` for many folders, in one statement.

    The same window over each folder's live children, partitioned by folder as
    well as by folded name, and the same rule: only a row whose answer changed
    is written. The caller must hold the folders' gate the way the single-folder
    form requires -- the tree report holds the leased folder, which every writer
    of a folder's children under the lease takes first.
    """
    if not parent_ids:
        return
    await repo.session.execute(
        text(
            "UPDATE file_nodes AS n SET flags_names = "
            "jsonb_set(n.flags_names, '{macos_safe}', to_jsonb(NOT s.collides)) "
            "FROM (SELECT id, count(*) OVER (PARTITION BY parent_id, name_key) > 1 AS collides "
            "FROM file_nodes WHERE parent_id = ANY(:parents) AND org_team_id = :org "
            "AND trashed_at IS NULL) AS s "
            "WHERE n.id = s.id AND n.org_team_id = :org "
            "AND n.flags_names -> 'macos_safe' IS DISTINCT FROM to_jsonb(NOT s.collides)"
        ),
        {"parents": list(parent_ids), "org": repo.scope.org_team_id},
    )


def _rename_inverse(node_id: NodeId, previous: bytes) -> Any:
    """The op module is imported here because it imports this one to apply."""
    from alkera_core.files.ops import RenameInverse

    return RenameInverse(node_id=node_id, name=previous.decode("latin-1"))


def _move_inverse(node_id: NodeId, was: NodeId, now: NodeId) -> Any:
    """See :func:`_rename_inverse` for why the import is local."""
    from alkera_core.files.ops import MoveInverse

    return MoveInverse(node_id=node_id, from_parent_id=was, to_parent_id=now)


__all__ = [
    "CONTAINER_KINDS",
    "FILE_NEEDS_CONTENT",
    "MAX_INLINE_MOVE_NODES",
    "ConflictBehavior",
    "HolderNode",
    "Namespace",
    "NodeAttrs",
    "insert_holder_nodes",
    "rederive_folding_flags_many",
]
