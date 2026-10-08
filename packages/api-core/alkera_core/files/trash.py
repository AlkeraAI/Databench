"""Trash, restore and purge: one sweep row per deletion, one statement per sweep.

Four rules shape this module.

*A deletion is one row and one statement.* Trashing a folder stamps ``trashed_at``
and ``trash_op_id`` on the whole subtree with a single ``UPDATE … WHERE path_ids
<@ …``, not a walk, so a 300,000-node folder costs the same round trips as a
one-node one, and no reader can ever observe half a trashed tree. The
``file_trash_ops`` row is what makes restore one operation instead of a
reconstruction: every node the sweep touched carries the op's id, so clearing it
is again a single statement.

*Deadlines come from Postgres, intervals from the injected clock.* ``purge_after``
is computed as ``now() + interval`` **inside** the statement, and the time left
on a trash op is likewise ``purge_after - now()`` read from the database. A
deadline written from the application's clock would be wrong for every other
process that reads it, which is exactly the class of bug a frozen-clock test
cannot see.

*Purge never touches the store.* It deletes the metadata rows for the subtree in
foreign-key order and leaves the objects behind: the version rows going away is
what makes those objects unreferenced, and the reachability sweep is the only
thing allowed to move bytes. A purge that deleted objects itself would race the
sweep's roots and could take bytes another node still points at.

*Every foreign key into the subtree has a named decision.* A key nobody decided
about surfaces as an unhandled ``ForeignKeyViolationError`` (a 500) when a user
asks for a file to be gone. So the purge answers each one, and the answer is
written down here:

==================================  ======  =======================================================
Foreign key                         Answer  Why
==================================  ======  =======================================================
``file_nodes.parent_id``            delete  The subtree goes deepest-first.
``file_nodes.target_id``            detach  A shortcut outside the subtree loses its target, not
                                            its row.
``file_drives.root_node_id``        refuse  A drive root is not deletable at all; ``trash`` refuses
                                            it and so does this.
``file_versions.node_id``           delete
``file_history.node_id``            delete  The node's own history dies with it; the org audit
                                            trail is a different table.
``file_dir_stats.node_id``          delete
``file_dir_stats_deltas.node_id``   delete
``file_shares.node_id``             delete
``file_stars.node_id``              delete
``file_conflicts.node_id``          delete  Takes its ``base``/``theirs``/``mine`` version keys
                                            with it: a conflict only ever names versions of its own
                                            node.
``file_ops.result_node_id``         detach  The operation happened; its record outlives what it
                                            produced.
``file_holds.node_id``              refuse  A live legal hold makes the subtree unpurgeable. This
                                            blocks the purge only: a trash is reversible and
                                            destroys no evidence, so a held node may be trashed and
                                            restored. A released hold is history and is deleted
                                            with the node.
``file_leases.node_id``             delete  Released: the node is gone, so there is nothing left
                                            for an epoch to fence.
``file_stage_jobs.lease_node_id``   delete  Before the lease it hangs off.
``file_lease_epoch_hwm.node_id``    delete  The mark guards epoch reuse for a node id, and ids are
                                            never reused.
``file_links.node_id``              delete  A public link to a purged node redeems to nothing; the
                                            row would keep a token hash alive for no one.
``file_locks.node_id``              delete
``file_upload_sessions.node_id``    delete  Aborted: the target is gone and it can never complete.
``file_upload_sessions.parent_id``  delete  Same, and the quota hold rides the row it is deleted
                                            with, so ``Sum(holds) == Sum(open sessions)`` still
                                            holds.
``file_content_grants.version_id``  refuse  An in-flight signed URL is a GC root, so
                                            purging under it hands a redeeming reader a version row
                                            that is gone. An expired or spent grant is deleted.
==================================  ======  =======================================================
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from alkera_core.authz.principal import ActingContext
from alkera_core.db.tenant_session import stepped_out
from alkera_core.files import history, names, stats
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock
from alkera_core.files.conflicts import conflict_rename
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import DriveId, NodeId, TrashOpId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import (
    LeaseContext,
    admitted_inbound,
    end_leases_under,
    fenced_write_for,
)
from alkera_core.files.namespace import rederive_folding_flags
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql
from alkera_core.files.store.keys import deleted_key
from alkera_core.files.store.scoped import DomainStore
from alkera_core.models.files.history import FileTrashOp
from alkera_core.models.files.tree import FileNode

if TYPE_CHECKING:  # the ops module imports this one to apply an inverse
    from alkera_core.files.ops import Operations, OperationState

#: How long a trashed subtree stays restorable before the purge sweeper may take
#: it. Written into ``purge_after`` at trash time so a later change of policy
#: does not retroactively shorten a window a user was promised.
TRASH_WINDOW: Final = timedelta(days=30)


def trash_left_behind_sql(source: str) -> str:
    """Two CTEs, ``ops`` then ``trashed``, that trash files a holder left behind.

    Every row ``source`` yields is trashed under a trash op of its own, which
    says why (``:reason``) and names the machine the bytes stayed on, so the
    trash lists it and a restore brings it back. This module owns every write
    that hides a node, so a sweeper that has to trash rows inside one of its
    own statements splices these in rather than spelling the update itself.

    ``source`` is a relation with the columns ``id`` (the node), ``drive_id``
    and ``machine``. The statement binds ``:org``, ``:actor``, ``:now``,
    ``:window`` (seconds) and ``:reason``. ``trashed`` returns ``id``,
    ``parent_id``, ``etag`` and ``op_id``. A node trashed for being left on a
    machine also stops carrying that machine's report: the facet describes a
    copy the drive has stopped waiting for.
    """
    return f"""ops AS (
    INSERT INTO file_trash_ops (id, org_team_id, drive_id, root_node_id, actor_id,
        deleted_at, purge_after, reason, reason_machine)
    SELECT gen_random_uuid(), :org, s.drive_id, s.id, :actor, :now,
        :now + make_interval(secs => :window), :reason, s.machine
    FROM {source} s
    RETURNING id, root_node_id
), trashed AS (
    UPDATE file_nodes n SET trashed_at = :now, trash_op_id = ops.id,
        holder_size = NULL, holder_mtime_ns = NULL, holder_hash = NULL, holder_seq = NULL,
        etag = n.etag + 1, updated_at = :now
    FROM ops WHERE n.id = ops.root_node_id AND n.org_team_id = :org
    RETURNING n.id, n.parent_id, n.etag, ops.id AS op_id
)"""  # noqa: S608


#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()

#: Tables whose rows hang off a node by ``node_id`` and must go before the node
#: itself. Ordered so a referent's dependents come first; ``file_nodes`` is
#: deleted separately, deepest first, because its foreign key points at itself.
#: Every foreign key into ``file_nodes``/``file_versions`` is accounted for in
#: the module docstring's table — a name missing here is a 500 on the purge, not
#: a leak, which is why the list is checked against the metadata by a test.
PURGE_TABLES: Final = (
    "file_history",
    "file_dir_stats_deltas",
    "file_dir_stats",
    "file_shares",
    "file_stars",
    "file_conflicts",
    "file_holds",
    "file_lease_live_entries",
    "file_leases",
    "file_lease_epoch_hwm",
    "file_links",
    "file_locks",
    "file_upload_sessions",
    "file_versions",
)

#: Rows that point into the subtree through a column that is not ``node_id``.
#: They are deleted before :data:`PURGE_TABLES` because each hangs off a row
#: that list is about to remove: a stage job off its lease, an upload session
#: off the parent folder it was going to land in.
PURGE_TABLES_INDIRECT: Final = (
    ("file_stage_jobs", "lease_node_id"),
    ("file_upload_sessions", "parent_id"),
)

#: Called with the subtree root before a trash lands. The lease service fills
#: this in: a subtree inside somebody else's lease may not be trashed, and the
#: refusal is ``files.leased``. Nothing here knows about leases, so the check is
#: a seam rather than an import.
LeaseCheck = Callable[[FileNode], Awaitable[None]]


async def _no_lease_check(node: FileNode) -> None:
    """The default hook: leases do not exist yet, so nothing is fenced."""


@dataclass(frozen=True, slots=True)
class TrashEntry:
    """One trashed root as the trash view shows it."""

    node: FileNode
    trash_op_id: TrashOpId
    #: Where it was when it was deleted, so "restore to original location" is
    #: answerable after the fact.
    original_parent_id: NodeId | None
    deleted_at: datetime
    purge_after: datetime
    #: ``purge_after - now()`` as Postgres computed it, never as Python did.
    time_left: timedelta
    #: Why the drive trashed it when nobody asked (``left_on_machine``), and the
    #: machine the reason names. ``None`` for a deletion somebody made.
    reason: str | None = None
    reason_machine: str | None = None


@dataclass(frozen=True, slots=True)
class TrashPage:
    """One keyset page of trashed roots."""

    entries: tuple[TrashEntry, ...]
    #: Feed back as ``marker`` for the next page; ``None`` at the end.
    next_marker: str | None


def _make_marker(deleted_at: datetime, op_id: uuid.UUID) -> str:
    """The cursor for the row after this one, in the listing's own order."""
    return f"{deleted_at.isoformat()}|{op_id}"


def _split_marker(marker: str | None) -> tuple[datetime | None, str | None]:
    """The two halves of a listing cursor, or two ``None`` for the first page.

    The timestamp comes back as a ``datetime`` because the driver binds a
    ``timestamptz`` parameter from one and refuses a string.

    The marker is this module's own string, so a caller that hands back
    something else is asking for a page that does not exist — answered as an
    invalid request rather than silently as page one, which a pager would walk
    forever.
    """
    if marker is None:
        return None, None
    at, sep, op = marker.partition("|")
    if not sep:
        raise InvalidRequest("trash marker is not a listing cursor")
    try:
        when = datetime.fromisoformat(at)
        uuid.UUID(op)
    except ValueError as error:
        raise InvalidRequest("trash marker is not a listing cursor") from error
    return when, op


def _name_columns(name: bytes) -> dict[str, Any]:
    """The derived name columns for one name."""
    try:
        name.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "binary"
    return {
        "name": name,
        "name_display": names.display(name),
        "name_key": names.name_key(name),
        "name_encoding": encoding,
    }


class Trash:
    """Deletion, undeletion and permanent removal for one tenant's tree."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        checkpoints: Checkpoints = NO_CHECKPOINTS,
        window: timedelta = TRASH_WINDOW,
        lease_check: LeaseCheck = _no_lease_check,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._window = window
        self._lease_check = lease_check
        self._quota = QuotaService(repo, ctx, clock, store)

    # ---- trash -----------------------------------------------------------

    async def trash(
        self,
        node_id: NodeId,
        *,
        if_match: int,
        op: Operations | None = None,
        lease: LeaseContext | None = None,
    ) -> FileTrashOp:
        """Move one node and everything under it to the trash.

        The precondition rides inside the subtree ``UPDATE`` as an ``EXISTS``
        over the root's ``etag``, so there is no window where the root moved on
        between a check and the sweep: either the whole subtree is stamped or
        nothing is.
        """
        node = await self._require(node_id)
        if node.parent_id is None:
            raise InvalidRequest("the drive root cannot be trashed")
        drive_id = DriveId(node.drive_id)
        await self._repo.lock_chain(
            drive_id, NodeId(node.parent_id), node_id, rewrites_subtree=True
        )
        await self._lease_check(node)
        # ``into`` stays false: a trash changes the node itself, so the lease
        # node and the working directory a machine is standing on refuse it
        # even when the lease takes inbound writes. Only something strictly
        # under the working directory is admitted, and the holder is told about
        # it on the live plane so its next drain unlinks the local copy.
        covering = await fenced_write_for(self._repo, node, lease)
        return await self._sweep(node, if_match=if_match, op=op, covering=covering)

    async def trash_for_deleted_object(
        self, node_id: NodeId, *, reason: str | None = None
    ) -> FileTrashOp | None:
        """Bin the node of an object the product has just deleted.

        The same deletion a member makes by hand (a trash op row, the whole
        subtree swept under it, a purge window, the delta), because it has to
        show up in the same Trash and be restorable from it. A bare ``UPDATE``
        on the root would produce a node the Trash listing cannot show (that
        listing joins through the op row), with no purge window and every file
        the object held still live under a trashed parent.

        Two things it does NOT do, and both are the point:

        No ``If-Match``. The object's own row carries the version this delete
        raced, and it has already won that race in the caller's transaction;
        the node's counter belongs to whoever last wrote the tree and is not a
        second vote on whether the object is deleted.

        No lease fence. Every other trash is somebody writing into a folder,
        and a folder with one writer refuses everyone else. This is not that:
        the subject of the lease is what is going away. The sweep ends the
        lease in this same transaction, the box's next beat is fenced and its
        re-take refused as ``files.trashed``, and the chat's own doorbell tells
        it to close the chat. Fencing it would mean a chat with a live box
        could never be deleted — the single case this path exists for.

        ``None`` when the node was trashed by someone else in between, so a
        racing member's own deletion is never undone by this one.
        """
        node = await self._fresh(node_id)
        if node is None or node.trashed_at is not None or node.parent_id is None:
            return None
        await self._repo.lock_chain(
            DriveId(node.drive_id), NodeId(node.parent_id), NodeId(node.id), rewrites_subtree=True
        )
        node = await self._reload(node_id)
        if node.trashed_at is not None:
            return None
        return await self._sweep(
            node, if_match=int(node.etag), op=None, covering=None, reason=reason
        )

    async def _sweep(
        self,
        node: FileNode,
        *,
        if_match: int,
        op: Operations | None,
        covering: Any | None,
        reason: str | None = None,
    ) -> FileTrashOp:
        """Write the trash op and stamp the subtree under it, in one savepoint.

        Shared by the member's own delete and by an object's, so the two cannot
        drift into producing different kinds of trashed node — the bug that made
        a deleted chat's folder unlistable was exactly that drift.
        """
        node_id = NodeId(node.id)
        drive_id = DriveId(node.drive_id)
        assert node.parent_id is not None

        subtree = await self._repo.nodes_by_path_prefix(node.path_ids)
        # A legal hold does not reach here: trashing is reversible, so it
        # destroys no evidence. Only the purge below answers to one.
        await self._refuse_if_held(subtree, legal_holds=False)
        await self._checkpoints.reach("trash.before_sweep")

        op_id = uuid.uuid4()
        savepoint = await self._repo.session.begin_nested()
        await self._repo.session.execute(
            text(
                "INSERT INTO file_trash_ops "
                "(id, org_team_id, drive_id, root_node_id, actor_id, deleted_at, purge_after) "
                "VALUES (:id, :org, :drive, :root, :actor, now(), now() + :window)"
            ),
            {
                "id": op_id,
                "org": self._repo.scope.org_team_id,
                "drive": drive_id,
                "root": node_id,
                "actor": self._actor(),
                "window": self._window,
            },
        )
        swept = await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET trashed_at = now(), trash_op_id = :op, "  # noqa: S608
                "etag = etag + 1, updated_at = now() "
                "WHERE org_team_id = :org "
                f"AND {subtree_sql('path_ids', 'CAST(:path AS ltree)')} "
                "AND trashed_at IS NULL AND EXISTS ("
                "  SELECT 1 FROM file_nodes AS r WHERE r.id = :id AND r.org_team_id = :org "
                "  AND r.etag = :if_match AND r.trashed_at IS NULL) "
                "RETURNING id"
            ),
            {
                "op": op_id,
                "org": self._repo.scope.org_team_id,
                "path": node.path_ids,
                "id": node_id,
                "if_match": if_match,
                **SUBTREE_DEPTH_BIND,
            },
        )
        if not swept.fetchall():
            await savepoint.rollback()
            raise PreconditionFailed(f"node {node_id} is not at etag {if_match}")
        await savepoint.commit()
        await self._checkpoints.reach("trash.after_sweep")
        # A trashed folder cannot be worked on, so nobody holds it any more:
        # the box running a deleted chat, a mount on a folder inside what was
        # just trashed. Left live, the lease was beaten on by a box that never
        # heard, and "Delete forever" was refused as somebody's local use of a
        # folder nobody could open. After the sweep, so the nodes are locked
        # before their lease rows.
        await end_leases_under(self._repo, node)

        if op is not None:
            await op.record_inverse(_restore_inverse(TrashOpId(op_id)))
        # Trashing the twin of a folding pair leaves the survivor safe again.
        await rederive_folding_flags(self._repo, node.parent_id)
        # The node leaves its folder's child count and nothing else: a trashed
        # node still holds its bytes in the store for the whole purge window,
        # so releasing them here would let a drive at its ceiling keep writing
        # by moving files to the trash. The bytes are charged back at purge,
        # which is also the only place the drive can come back under it.
        await self._delta(subtree, NodeId(node.parent_id), sign=-1, bytes_and_files=False)
        await self._announce(
            node_id,
            drive_id,
            kind_of_change="trash",
            before={"parent_id": str(node.parent_id)},
            # One deletion, one history row. A caller with a reason of its own —
            # an object going away rather than a member emptying a folder — says
            # so here rather than writing a second ``trash`` row beside this one,
            # which is two deletions as far as any reader of the history is
            # concerned.
            after=(
                {"trash_op_id": str(op_id)}
                if reason is None
                else {"trash_op_id": str(op_id), "metadata": {"reason": reason}}
            ),
        )
        if admitted_inbound(covering) is not None:
            await LiveEntriesService(self._repo, self._ctx).accept_inbound(
                node, kind="inbound_delete"
            )
        written = await self._repo.trash_op(op_id)
        assert written is not None
        return written

    # ---- restore ---------------------------------------------------------

    async def restore(
        self,
        trash_op_id: TrashOpId,
        *,
        parent_id: NodeId | None = None,
        lease: LeaseContext | None = None,
        operation: Operations | None = None,
    ) -> FileNode | OperationState:
        """Bring one trash op's subtree back, whole.

        The subtree is un-stamped by ``trash_op_id`` in one statement — the same
        set the sweep marked, so a node that was already trashed when the sweep
        ran stays trashed and a node trashed later by another op is untouched.
        Only the root can need a new name or a new parent, and only the root's
        row is rewritten for it.

        A restore that has to land the subtree somewhere else can be oversized,
        and then the re-parent is a queued operation rather than a write. That
        operation is returned instead of the node, exactly as ``move`` returns
        it: the caller is the only one who can start the runner, and a node
        returned here would say the subtree had already arrived.
        """
        op = await self._repo.trash_op(trash_op_id)
        if op is None:
            raise NotFound(f"no trash op {trash_op_id}")
        root = await self._reload(NodeId(op.root_node_id))
        if root.trashed_at is None or root.trash_op_id != op.id:
            raise InvalidRequest(f"trash op {trash_op_id} has already been restored")

        target = await self._restore_parent(op, root, parent_id)
        # The store first, with nothing locked: recovering a large subtree asks
        # the store about every version in it, and the namespace taken below
        # stops every create in the drive for as long as it is held. An object
        # recovered for a restore that then fails is parked again by the next
        # sweep, exactly as it was.
        await self._recover_parked_objects(root)
        # The folder gate, over both folders this restore writes: the root's
        # row is rewritten (the conflict rename below) while it still stands in
        # the folder it was trashed from, and the folding flags of the folder
        # it lands in are re-derived at the end. The namespace (exclusively: a
        # restore re-roots the subtree), then the two parents by ascending id,
        # then the root — the same total order the move uses, so a restore and
        # a move that touch one pair of folders cannot take them in opposite
        # orders.
        await self._repo.lock_namespace(DriveId(op.drive_id), exclusive=True)
        folders = {NodeId(target.id)}
        if root.parent_id is not None:
            folders.add(NodeId(root.parent_id))
        for folder_id in sorted(folders):
            await self._repo.lock_node(folder_id)
        held = await self._repo.lock_node(NodeId(root.id))
        # Read again under the lock: another restore of the same op may have
        # finished while this one was asking the store.
        if held is None or held.trashed_at is None or held.trash_op_id != op.id:
            raise InvalidRequest(f"trash op {trash_op_id} has already been restored")
        # After the policy decision and after the chain lock, inside the
        # transaction that writes: putting a subtree back into somebody else's
        # mount is their write to make, and the lease row it fences on sits
        # after the drive and the nodes in the fixed order. A restore lands a
        # child IN ``target``, so an awake chat admits it into its working
        # directory like any other drop — which is what lets a person undo a
        # conflicted copy they trashed there — and its holder is told below.
        covering = await fenced_write_for(self._repo, target, lease, into=True)
        # The names both folders hold, not just the destination's. The root is
        # un-trashed while it still stands in the folder it was trashed from —
        # the re-parent happens after — so a name free only at the destination
        # can still collide with a live sibling at the source and the un-trash
        # aborts on the live-sibling index. The union is what makes that
        # intermediate state legal; the extra names only push the conflict
        # index up, never make one unavailable at the destination. Same reason,
        # same shape as ``move``'s rename branch.
        source: Sequence[FileNode] = (
            await self._repo.siblings(NodeId(root.parent_id)) if root.parent_id is not None else ()
        )
        taken = {
            row.name
            for row in (*await self._repo.siblings(NodeId(target.id)), *source)
            if row.id != root.id
        }
        name = conflict_rename(root.name, taken.__contains__)
        await self._checkpoints.reach("trash.before_restore")

        if name != root.name:
            await self._repo.session.execute(
                text(
                    "UPDATE file_nodes SET name = :name, name_display = :name_display, "
                    "name_key = :name_key, name_encoding = :name_encoding, "
                    "etag = etag + 1, updated_at = now() "
                    "WHERE id = :id AND org_team_id = :org"
                ),
                {
                    "id": root.id,
                    "org": self._repo.scope.org_team_id,
                    **_name_columns(name),
                },
            )
        await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET trashed_at = NULL, trash_op_id = NULL, "
                "etag = etag + 1, updated_at = now() "
                "WHERE org_team_id = :org AND trash_op_id = :op"
            ),
            {"org": self._repo.scope.org_team_id, "op": op.id},
        )
        await self._repo.session.execute(
            text("DELETE FROM file_trash_ops WHERE id = :op AND org_team_id = :org"),
            {"op": op.id, "org": self._repo.scope.org_team_id},
        )
        await self._checkpoints.reach("trash.after_restore")

        restored = await self._reload(NodeId(root.id))
        if admitted_inbound(covering) is not None and restored.kind == "file":
            await LiveEntriesService(self._repo, self._ctx).accept_inbound(restored, kind="inbound")
        queued: OperationState | None = None
        if target.id != root.parent_id:
            from alkera_core.files.namespace import Namespace
            from alkera_core.files.ops import OperationState

            namespace = Namespace(self._repo, self._ctx, self._clock, self._store)
            # An oversized subtree makes ``move`` hand back the operation it
            # queued instead of a node. Returning the un-moved row here would
            # report a restore that has not happened and leave a queued
            # operation nobody starts, so the operation is handed to the caller.
            moved = await namespace.move(
                NodeId(root.id), NodeId(target.id), if_match=restored.etag, lease=lease
            )
            if isinstance(moved, OperationState):
                queued = moved
            restored = await self._reload(NodeId(root.id))

        # Where the subtree actually stands: a queued re-parent has not run, so
        # the folding rederive and the child count belong to the folder the root
        # is still in, not to the one it is on its way to.
        landed = NodeId(restored.parent_id) if restored.parent_id is not None else NodeId(target.id)
        # A restore puts the twin back, so the pair is unsafe on macOS again.
        await rederive_folding_flags(self._repo, landed)
        subtree = await self._repo.nodes_by_path_prefix(restored.path_ids)
        # The mirror of the trash: only the child count comes back, because the
        # bytes never left the count while the subtree sat in the trash.
        await self._delta(subtree, landed, sign=1, bytes_and_files=False)
        if operation is not None:
            await operation.record_inverse(_trash_inverse(NodeId(root.id)))
        await self._announce(
            NodeId(root.id),
            DriveId(op.drive_id),
            kind_of_change="restore",
            before={"trash_op_id": str(op.id)},
            after={"parent_id": str(landed)},
        )
        return queued if queued is not None else restored

    async def _recover_parked_objects(self, root: FileNode) -> None:
        """Bring every object the two-phase delete parked back before the swap.

        The reachability sweep does not erase an unreferenced object, it *moves*
        it to ``deleted/<key>`` and leaves it there for a week — so a subtree
        restored in the last minutes of its window can have live version rows
        whose bytes are one rename away from where the row says they are. Undoing
        that rename is part of the restore, not a repair somebody runs later: a
        restore that returned rows pointing at nothing would hand the user a file
        that reads as zeros.

        Every key is decided before any key moves. A restore that cannot recover
        one object refuses with nothing moved and nothing un-trashed, because a
        half-restored tree — some files whole, some empty — is the outcome this
        whole exercise exists to prevent.
        """
        if self._store is None:
            return
        subtree = await self._repo.nodes_by_path_prefix(root.path_ids)
        rows = (
            await self._repo.session.execute(
                text(
                    "SELECT id, store_key FROM file_versions "
                    "WHERE org_team_id = :org AND node_id = ANY(:ids) "
                    "AND store_key IS NOT NULL"
                ),
                {
                    "org": self._repo.scope.org_team_id,
                    "ids": [row.id for row in subtree],
                },
            )
        ).all()

        parked: dict[str, uuid.UUID] = {}
        for version_id, key in rows:
            if key in parked or await self._store.head(key) is not None:
                continue
            if await self._store.head(deleted_key(key)) is None:
                raise NotFound(f"version {version_id} has no object at {key}: nothing was restored")
            parked[key] = version_id
        if not parked:
            return

        await self._checkpoints.reach("trash.before_recover")
        for key in parked:
            await self._store.move(deleted_key(key), key)
        for key, version_id in parked.items():
            if await self._store.head(key) is None:
                raise NotFound(f"version {version_id}'s object {key} did not survive its recovery")
        await self._checkpoints.reach("trash.after_recover")

    async def _restore_parent(
        self, op: FileTrashOp, root: FileNode, parent_id: NodeId | None
    ) -> FileNode:
        """Where the subtree lands: the asked-for folder, or back on its path.

        When the caller names no parent the subtree goes to its original parent
        if that folder is still live, and otherwise to the *nearest live
        ancestor* on its original path — the closest surviving folder, which is
        where the user last saw the tree and is what restoring a whole trashed
        branch one op at a time has to mean. Only when no ancestor survives does
        it fall back to the actor's home folder or the drive root. Never
        nowhere: a restore that cannot find a parent would leave the subtree
        unreachable, which is worse than a surprising location.
        """
        if parent_id is not None:
            asked = await self._fresh(parent_id)
            if asked is None or asked.trashed_at is not None:
                raise NotFound(f"no node {parent_id}")
            if asked.kind != "folder":
                raise InvalidRequest(f"a {asked.kind} may not hold children")
            return asked
        original = None if root.parent_id is None else await self._fresh(NodeId(root.parent_id))
        if original is not None and original.trashed_at is None:
            return original
        nearest = await self._nearest_live_ancestor(root)
        if nearest is not None:
            return nearest
        return await self._home(DriveId(op.drive_id))

    async def _nearest_live_ancestor(self, root: FileNode) -> FileNode | None:
        """The closest surviving folder above ``root`` on its original path.

        Trashing stamps ``trashed_at`` and leaves ``path_ids`` alone, so the
        original path is still readable off the trashed row and the walk is one
        chain read from the nearest ancestor outwards.

        Each candidate is read back before it is believed, for the same reason
        :meth:`_fresh` exists: the sweep that trashed the branch is a statement
        the mapper never saw, so an ancestor the session already held still
        looks live. Taking one at its word would land the restored subtree
        inside a folder that is in the trash.
        """
        for candidate in reversed(await self._repo.chain(root)):
            if candidate.id == root.id:
                continue
            current = await self._fresh(NodeId(candidate.id))
            if current is None:
                continue
            if current.trashed_at is None and current.kind == "folder":
                return current
        return None

    async def _home(self, drive_id: DriveId) -> FileNode:
        """The actor's home folder, or the drive root when there is none."""
        drive = await self._repo.drive(drive_id)
        if drive is None or drive.root_node_id is None:
            raise NotFound(f"no drive {drive_id}")
        landing = await self._require(NodeId(drive.root_node_id))
        for segment in (b"home", str(self._actor()).encode()):
            found = [
                row
                for row in await self._repo.siblings(NodeId(landing.id))
                if row.name == segment and row.kind == "folder"
            ]
            if not found:
                return landing
            landing = found[0]
        return landing

    # ---- purge -----------------------------------------------------------

    async def purge(self, node_id: NodeId, *, lease: LeaseContext | None = None) -> None:
        """Delete one subtree's rows for good.

        Nothing is asked of the store: the version rows going away is what makes
        their objects unreferenced, and the reachability sweep — which knows
        about the other roots that might still point at the same bytes — is what
        eventually removes them.
        """
        node = await self._require(node_id)
        if node.parent_id is None:
            raise InvalidRequest("the drive root cannot be purged")
        if node.trashed_at is not None:
            # A lease on what is in the trash is one a trash from before leases
            # ended with it left behind, or one a holder kept beating after. It
            # holds nothing anybody may write, so it does not stand between the
            # person and deleting it forever. The folder's gate first, then its
            # lease rows, in the fixed order; a lease on a live folder ABOVE
            # this one is untouched, and the fence below still answers to it.
            await self._repo.lock_lease_gate(node_id)
            await end_leases_under(self._repo, node)
        await fenced_write_for(self._repo, node, lease)
        subtree = await self._repo.nodes_by_path_prefix(node.path_ids)
        await self._refuse_if_held(subtree, legal_holds=True)
        ids = [row.id for row in subtree]
        await self._refuse_if_grant_in_flight(ids)
        await self._checkpoints.reach("trash.before_purge")

        org = self._repo.scope.org_team_id
        await self._detach_references(ids, org)
        await self._repo.session.execute(
            text(
                "DELETE FROM file_content_grants WHERE org_team_id = :org AND version_id IN ("
                "SELECT id FROM file_versions WHERE org_team_id = :org AND node_id = ANY(:ids))"
            ),
            {"ids": ids, "org": org},
        )
        for table, column in PURGE_TABLES_INDIRECT:
            # Same interpolation rule as below: both halves come from the
            # module-level tuple, never from a caller.
            await self._repo.session.execute(
                text(
                    f"DELETE FROM {table} WHERE {column} = ANY(:ids) AND org_team_id = :org"  # noqa: S608 - the only interpolation is PURGE_TABLES_INDIRECT, a literal tuple
                ),
                {"ids": ids, "org": org},
            )
        for table in PURGE_TABLES:
            # The table name is interpolated because a parameter cannot name a
            # relation; every name comes from the module-level tuple above, so
            # nothing a caller supplies reaches the statement.
            await self._repo.session.execute(
                text(
                    f"DELETE FROM {table} WHERE node_id = ANY(:ids) AND org_team_id = :org"  # noqa: S608 - the only interpolation is PURGE_TABLES, a literal tuple
                ),
                {"ids": ids, "org": org},
            )
        await self._repo.session.execute(
            text(
                "DELETE FROM file_nodes WHERE id = ANY(:ids) AND org_team_id = :org "
                "AND NOT EXISTS (SELECT 1 FROM file_nodes AS c WHERE c.parent_id = file_nodes.id "
                "AND c.id <> ALL(:ids))"
            ),
            {"ids": ids, "org": org},
        )
        await self._repo.session.execute(
            text(
                "DELETE FROM file_trash_ops WHERE org_team_id = :org AND NOT EXISTS ("
                "SELECT 1 FROM file_nodes WHERE file_nodes.trash_op_id = file_trash_ops.id)"
            ),
            {"org": org},
        )
        await self._forget_live_documents(ids, org)
        await self._checkpoints.reach("trash.after_purge")
        # Purge is where the bytes are actually released: trashing left them
        # counted because the store still held them. The child count is only
        # charged here for a node that never went through the trash, which
        # already took it.
        counted = [row for row in subtree if row.kind == "file" and row.head_version_id is not None]
        await stats.add_delta(
            self._repo,
            node_id=NodeId(node.parent_id),
            bytes_delta=-sum(row.size for row in counted),
            files_delta=-len(counted),
            direct_children_delta=0 if node.trashed_at is not None else -1,
            child_change_at=self._clock.now(),
        )
        # The bytes are gone, so this is the write that can bring a drive back
        # under its ceiling.
        await self._quota.thaw_if_under(DriveId(node.drive_id))

    async def _forget_live_documents(self, ids: Sequence[uuid.UUID], org: uuid.UUID) -> None:
        """A file's live document holds its text too (and its history of every
        edit): purging the file deletes it with the file, in the same
        transaction, along with who held a peer on it.

        ``crdt_*`` are platform tables the Files role holds no grant on, so
        this steps out of it for the two statements (both carry the org
        themselves) and puts back the role that was in force after. It steps
        out to the tenant role a bound request runs under, so the content
        tables' policy still binds, and to the login only where the session
        already ran as the login (a sweep, a cross-tenant window).
        """
        session = self._repo.session
        docs = [str(node) for node in ids]
        async with stepped_out(session):
            for table in ("crdt_peers", "crdt_docs"):
                await session.execute(
                    text(
                        f"DELETE FROM {table} WHERE org_id = :org AND doc_type = 'file' "  # noqa: S608 - two literal table names
                        "AND doc_id = ANY(:docs)"
                    ),
                    {"org": org, "docs": docs},
                )

    async def _detach_references(self, ids: Sequence[uuid.UUID], org: uuid.UUID) -> None:
        """Drop the two pointers that outlive nothing, before the rows go.

        Both are nulled here rather than made ``ON DELETE SET NULL`` in the
        schema, and for the same reason: purge is the only place either row may
        legitimately lose what it points at, and a cascade would make every
        other delete path silently rewrite them instead of failing.

        ``file_nodes.head_version_id`` names the node's live head. A cascade
        there would let a version pruner or a botched delete leave a live file
        headless — reading as absent rather than refusing — which is the exact
        class of silent data loss the version chain exists to prevent. Purge is
        deleting the node in the same transaction, so its head means nothing a
        moment later.

        ``file_ops.result_node_id`` is the record of what an operation
        produced. The row itself must survive the purge — the operation
        happened, and its history is not the user's to erase by deleting a
        file — but a foreign key cannot point at a row that is gone, so the
        pointer is dropped and the audit trail keeps everything else.

        ``file_nodes.target_id`` is a shortcut's referent. A shortcut *inside*
        the subtree is deleted with it, so only one outside needs anything: it
        keeps its row and becomes a shortcut to nothing, which is what a user
        who deleted the target asked for. The ``id <> ALL(:ids)`` keeps this
        from writing rows the delete below is about to take anyway.
        """
        await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET head_version_id = NULL "
                "WHERE id = ANY(:ids) AND org_team_id = :org AND head_version_id IS NOT NULL"
            ),
            {"ids": list(ids), "org": org},
        )
        await self._repo.session.execute(
            text(
                "UPDATE file_ops SET result_node_id = NULL "
                "WHERE result_node_id = ANY(:ids) AND org_team_id = :org"
            ),
            {"ids": list(ids), "org": org},
        )
        await self._repo.session.execute(
            text(
                "UPDATE file_nodes SET target_id = NULL "
                "WHERE target_id = ANY(:ids) AND org_team_id = :org AND id <> ALL(:ids)"
            ),
            {"ids": list(ids), "org": org},
        )

    async def empty(self, drive_id: DriveId, *, lease: LeaseContext | None = None) -> int:
        """Purge every trashed root in one drive. Returns how many it removed.

        A held subtree refuses, and because the whole thing is one transaction a
        refusal leaves the trash exactly as it was rather than half emptied.
        """
        page = await self.list_trash(drive_id, marker=None, limit=1_000)
        removed = 0
        for entry in page.entries:
            await self.purge(NodeId(entry.node.id), lease=lease)
            removed += 1
        return removed

    # ---- reads -----------------------------------------------------------

    async def list_trash(
        self, drive_id: DriveId, *, marker: str | None = None, limit: int = 50
    ) -> TrashPage:
        """One page of trashed roots, newest deletion first.

        A root is a node that carries a trash op whose ``root_node_id`` is
        itself, so a folder that was trashed with its parent contributes nothing
        to the page: the trash view shows one entry per deletion.

        The order is what a person just did, first: a trash that ordered by op id
        put a deletion made a moment ago behind a hundred older ones, and with a
        page size of 50 that made every fresh deletion unreachable. ``deleted_at``
        alone is not a key — two roots trashed in the same statement share it — so
        the id breaks the tie and the marker carries both.
        """
        self._repo._require_open()
        at, op = _split_marker(marker)
        rows = (
            await self._repo.session.execute(
                text(
                    "SELECT o.id AS op_id, o.root_node_id, o.deleted_at, o.purge_after, "
                    "o.purge_after - now() AS time_left, n.parent_id, "
                    "o.reason, o.reason_machine "
                    "FROM file_trash_ops AS o JOIN file_nodes AS n ON n.id = o.root_node_id "
                    "WHERE o.org_team_id = :org AND o.drive_id = :drive "
                    "AND n.trash_op_id = o.id "
                    "AND (CAST(:marker_at AS timestamptz) IS NULL "
                    "OR (o.deleted_at, o.id) "
                    "< (CAST(:marker_at AS timestamptz), CAST(:marker_op AS uuid))) "
                    "ORDER BY o.deleted_at DESC, o.id DESC LIMIT :limit"
                ),
                {
                    "org": self._repo.scope.org_team_id,
                    "drive": drive_id,
                    "marker_at": at,
                    "marker_op": op,
                    "limit": limit,
                },
            )
        ).mappings()
        found = list(rows)
        nodes = {
            row.id: row
            for row in await self._repo.nodes([NodeId(item["root_node_id"]) for item in found])
        }
        entries = tuple(
            TrashEntry(
                node=nodes[item["root_node_id"]],
                trash_op_id=TrashOpId(item["op_id"]),
                original_parent_id=None if item["parent_id"] is None else NodeId(item["parent_id"]),
                deleted_at=item["deleted_at"],
                purge_after=item["purge_after"],
                time_left=item["time_left"],
                reason=item["reason"],
                reason_machine=item["reason_machine"],
            )
            for item in found
            if item["root_node_id"] in nodes
        )
        marker_out = (
            _make_marker(found[-1]["deleted_at"], found[-1]["op_id"])
            if len(found) == limit and found
            else None
        )
        return TrashPage(entries=entries, next_marker=marker_out)

    # ---- internals -------------------------------------------------------

    def _actor(self) -> uuid.UUID:
        """Whose trash operation this is: the human behind an agent, else the
        caller, folded so a non-UUID id keeps its own identity.

        A nil-UUID fallback would make every service principal the same actor
        with the same ``/home/00000000-…`` landing folder.
        """
        return history.subject_ref(self._ctx)

    async def _require(self, node_id: NodeId) -> FileNode:
        node = await self._repo.node(node_id)
        if node is None:
            raise NotFound(f"no node {node_id}")
        return node

    async def _fresh(self, node_id: NodeId) -> FileNode | None:
        """Read a node as the database has it now, or ``None``.

        The mapper hands back the instance a previous transaction loaded, whose
        ``trashed_at`` may predate a sweep this method exists to notice — so the
        row is refreshed before it is believed.
        """
        node = await self._repo.node(node_id)
        if node is not None:
            await self._repo.session.refresh(node)
        return node

    async def _reload(self, node_id: NodeId) -> FileNode:
        """Re-read a row the raw statements above changed under the mapper."""
        node = await self._require(node_id)
        await self._repo.session.refresh(node)
        return node

    async def _refuse_if_held(self, subtree: Sequence[FileNode], *, legal_holds: bool) -> None:
        """Refuse the whole operation when anything in the subtree is held.

        A hold is on the subtree, not on the node the caller named: deleting the
        parent of a held file would take the held file with it, so the refusal
        has to consider everything the statement would touch. The state is read
        back from the database rather than off the instances the caller already
        holds. A hold placed by another transaction is exactly the case this
        exists for, and a mapper's cached row would not show it.

        ``legal_holds`` is the axis a live ``file_holds`` row sits on, and only
        a permanent deletion sets it. A legal hold preserves evidence: nothing
        under it is ever purged, and its bytes never change. It is not a
        freeze of the namespace: a trash is reversible and loses no
        version, so a held file may be moved to the trash and restored, and
        refusing that would let a matter reference make a folder undeletable
        for the whole org. The purge that would actually destroy the row is
        where the hold wins, and where a released hold becomes history that is
        deleted with the node.
        """
        ids = [row.id for row in subtree]
        clauses = [
            "SELECT 'node' FROM file_nodes WHERE org_team_id = :org "
            "AND id = ANY(:ids) AND state = 'locked'",
            "SELECT 'version' FROM file_versions WHERE org_team_id = :org "
            "AND node_id = ANY(:ids) AND (held OR keep_forever)",
        ]
        if legal_holds:
            clauses.append(
                "SELECT 'legal hold' FROM file_holds WHERE org_team_id = :org "
                "AND node_id = ANY(:ids) AND released_at IS NULL"
            )
        found = (
            await self._repo.session.execute(
                text(" UNION ALL ".join(clauses) + " LIMIT 1"),
                {"org": self._repo.scope.org_team_id, "ids": ids},
            )
        ).scalar()
        if found is not None:
            raise Conflict("files.held", f"a held {found} refuses this operation")

    async def _refuse_if_grant_in_flight(self, ids: Sequence[uuid.UUID]) -> None:
        """Refuse a purge while a signed content URL into the subtree is live.

        A grant is a GC root precisely because the bytes behind it must survive
        until it is spent or lapses; purging the version row under a reader who
        is holding a URL would turn a legitimate download into a 500 on a
        dangling key. So the grant wins and the purge comes back — the sweeper
        that drives it retries on the next pass, by which time the grant has
        expired.

        Liveness is Postgres's ``now()``, not the injected clock: the grant was
        written with a database deadline and every other reader of it compares
        against the same one.
        """
        live = (
            await self._repo.session.execute(
                text(
                    "SELECT 1 FROM file_content_grants g JOIN file_versions v "
                    "ON v.id = g.version_id AND v.org_team_id = g.org_team_id "
                    "WHERE g.org_team_id = :org AND v.node_id = ANY(:ids) "
                    "AND g.used_at IS NULL AND g.expires_at > now() LIMIT 1"
                ),
                {"org": self._repo.scope.org_team_id, "ids": list(ids)},
            )
        ).scalar()
        if live is not None:
            raise Conflict("files.held", "an in-flight content grant refuses this operation")

    async def _delta(
        self,
        subtree: Sequence[FileNode],
        parent_id: NodeId,
        *,
        sign: int,
        bytes_and_files: bool = True,
    ) -> None:
        """Append the folder-stats increment for a whole subtree leaving or arriving.

        Trash and restore append mirror images of the same numbers, so a
        round trip nets to zero and a folder's size is never counted twice.

        ``bytes_and_files=False`` moves only the direct-child count, which is
        what trash and restore charge: a trashed node keeps its ``size`` and its
        versions, so its bytes are still held and still count against the
        drive's ceiling. Purge is where they are released, and it is the only
        caller that charges bytes without a child.

        Only what a recount of ``file_versions`` counts is moved — a file with a
        head version. A headless node was never charged (``namespace.create``
        charges it 0), so taking its ``size`` back here would push the aggregate
        permanently below the truth.
        """
        counted = [row for row in subtree if row.kind == "file" and row.head_version_id is not None]
        await stats.add_delta(
            self._repo,
            node_id=parent_id,
            bytes_delta=sign * sum(row.size for row in counted) if bytes_and_files else 0,
            files_delta=sign * len(counted) if bytes_and_files else 0,
            direct_children_delta=sign,
            child_change_at=self._clock.now(),
        )

    async def _announce(
        self,
        node_id: NodeId,
        drive_id: DriveId,
        *,
        kind_of_change: history.HistoryKind,
        before: history.HistorySnapshot | None,
        after: history.HistorySnapshot | None,
    ) -> None:
        """The history row and the outbox row, in the caller's transaction."""
        # Refreshed, not merely mapped: the sweep above ran as raw statements,
        # so the instance the session still holds carries the etag from before
        # the trash. Announcing that one would publish a version no read of the
        # node returns, and a consumer chaining etags would stall on this node.
        node = await self._fresh(node_id)
        await history.record(
            self._repo,
            self._ctx,
            node_id=node_id,
            kind=kind_of_change,
            before=before,
            after=after,
        )
        # The folder rides the row so the change feed can still vouch for a
        # tombstone after a purge has taken the node's own row away.
        await history.emit_node_changed(
            self._repo,
            self._ctx,
            node_id=node_id,
            drive_id=drive_id,
            version=0 if node is None else node.etag,
            parent_id=None if node is None or node.parent_id is None else NodeId(node.parent_id),
        )


def _restore_inverse(trash_op_id: TrashOpId) -> Any:
    """Imported locally: ``ops`` imports this module to apply an inverse."""
    from alkera_core.files.ops import RestoreInverse

    return RestoreInverse(trash_op_id=trash_op_id)


def _trash_inverse(node_id: NodeId) -> Any:
    """See :func:`_restore_inverse` for why the import is local."""
    from alkera_core.files.ops import TrashInverse

    return TrashInverse(node_id=node_id)


__all__ = [
    "PURGE_TABLES",
    "PURGE_TABLES_INDIRECT",
    "TRASH_WINDOW",
    "LeaseCheck",
    "Trash",
    "TrashEntry",
    "TrashPage",
]
