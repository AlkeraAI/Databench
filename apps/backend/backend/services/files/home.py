"""The caller's own home folder — the one place ``/files`` is allowed to land.

``/home`` is a traversal-only *container*: it holds every member's home folder
and grants nothing itself. It is a signpost, not a workspace, so no client may
treat it as "my files" — an org admin sits at ``manager`` on every drive of her
org, so a listing of the container hands her every colleague's home folder.
What "my files" means is ``/home/<me>``, and only the server can say which node
that is, so only the node id crosses to the client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog
from alkera_core.files import drives
from alkera_core.files.errors import FilesError
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.namespace import Namespace
from alkera_core.files.objects_bridge import (
    FolderObjectKind,
    chat_templates_folder,
    chats_folder,
)
from alkera_core.files.ops import QUEUED_STALE_AFTER, Operations, OperationState
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from fastapi import BackgroundTasks

from backend.services.files.context import FilesContext
from backend.services.files.operations_runner import queue_inline

log = structlog.get_logger(__name__)

#: The public door to each place a folder object is filed in under its owner's
#: home, by the folder name the bridge's registry gives that kind. A kind that
#: registers a new place adds its door here; the routes stay one path.
_HOME_PLACES: dict[bytes, Callable[[FilesRepo, Namespace, FileNode], Awaitable[FileNode]]] = {
    drives.CHATS_NAME: chats_folder,
    drives.CHAT_TEMPLATES_NAME: chat_templates_folder,
}


async def home_place(files: FilesContext, kind: FolderObjectKind, home: FileNode) -> FileNode:
    """The folder under ``home`` this kind is filed in, created when missing.

    A kind that names no place — or one whose door is not registered above — is
    filed in the home itself rather than somewhere invented for it.
    """
    door = _HOME_PLACES.get(kind.home_folder_name or b"")
    if door is None:
        return home
    namespace = Namespace(files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings)
    place: FileNode = await door(files.repo, namespace, home)
    return place


async def caller_home(
    files: FilesContext, *, background: BackgroundTasks | None = None
) -> FileNode | None:
    """``/home/<me>`` for whoever is calling, created on this visit if it is missing.

    The home is found by its identity — a folder under ``home/`` the caller
    created under the name their id derives
    (:func:`alkera_core.files.drives.lookup_home`) — never by taking whatever
    the caller happens to have put there. A folder she made under the container
    called ``Test`` is not her home, however early she made it; her home is
    ensured beside it.

    Ensuring here rather than only on membership is what gives the *founder* a
    home: an org's first member never goes through ``add_member`` — she creates
    the org — so her home would otherwise never be written and her Files landing
    screen would hold nothing of hers. It is the same idempotent call membership
    makes, and it is safe on every drive read: an existing home is found by id
    and nothing is written.

    Whatever else the caller created straight into the container — the ``Test``
    folder, a receipt uploaded before the drive named her home — is moved into
    the home on this read, so the things she made are where she looks for them
    rather than beside a signpost only an admin can list. Only her own rows
    move, never a home, and once moved there is nothing left to move. A subtree
    too large to move inside a request goes the way an oversized move always
    goes: a queued operation, run after this response by ``background`` on a
    deployment that runs its operations inline and by the worker otherwise, so
    the drive read is never held behind a path rewrite.

    A second folder that is the caller's home by its owner grant but not by
    name — one an earlier release ensured under the caller's address, which a
    task still on that release can write during a rolling deploy — is never
    nested: its contents are moved into the home and the empty folder goes
    (:func:`_merge`), so the caller ends with the one home, ``/home/<id>``.

    ``None`` when the caller is not a user (a CI or proxy token has no home) —
    such a principal gets a drive with no home to land in, not somebody else's.
    """
    me = files.ctx.effective_user_id
    if me is None:
        return None
    async with files.repo.transaction():
        lookup = await drives.lookup_home(files.repo, me, drive=files.drive)
    home = lookup.home
    if home is None:
        async with files.repo.transaction():
            home = await drives.ensure_home_folder(files.repo, files.ctx, me)
    for extra in lookup.extras:
        await _merge(files, home, extra, background=background)
    if lookup.strays:
        await _adopt(files, home, lookup.strays, background=background)
    return home


async def _adopt(
    files: FilesContext,
    home: FileNode,
    strays: tuple[FileNode, ...],
    *,
    background: BackgroundTasks | None,
) -> None:
    """Move what the caller left directly under ``home/`` into their home.

    Each move is the library's own — the subtree's paths are rewritten, the
    cached ACLs are invalidated, and the history row and outbox event every
    mutation owes are written — under a name that yields to whatever the home
    already holds. One transaction per stray, so a stray that cannot move (a
    lease over it, a queued operation already carrying it) is logged and left
    for the next read rather than costing the caller their drive.

    A subtree past the inline budget comes back as a queued ``move`` operation
    instead of a moved node. It is handed to the same runner the move route
    hands its oversized moves to, and a later read finds the operation still
    carrying the node and queues no second one.

    Two reads can overlap — a client retrying a read that a 24k-node subtree
    made slow — so the check and the queueing happen under the stray's row
    lock, taken in the one order every Files mutation takes (drive, then
    node): the second read waits for the first's answer and then finds it,
    rather than reading past it under READ COMMITTED and queueing its own.

    A queued operation nobody claimed within :data:`QUEUED_STALE_AFTER` was
    handed to a runner that never came, so this read hands it over again:
    the claim is a compare-and-swap, so the hand-off is idempotent, and every
    other queued operation carrying the same node is closed as superseded
    first so the one move happens once.
    """
    namespace = Namespace(files.repo, files.ctx, files.clock, files.store)
    operations = Operations(files.repo, files.ctx, files.clock, files.store)
    for stray in strays:
        pending: OperationId | None = None
        try:
            async with files.repo.transaction():
                await files.repo.lock_chain(
                    DriveId(home.drive_id), None, NodeId(stray.id), rewrites_subtree=True
                )
                carrying = await files.repo.live_operation_for(NodeId(stray.id), kind="move")
                if carrying is not None:
                    if not _abandoned(carrying, files.clock.now()):
                        log.info(
                            "files.home.adopt_already_queued",
                            node_id=str(stray.id),
                            op_id=str(carrying.id),
                        )
                        continue
                    pending = OperationId(carrying.id)
                    closed = await operations.supersede_queued(pending)
                    log.info(
                        "files.home.adopt_rehanded",
                        node_id=str(stray.id),
                        op_id=str(pending),
                        superseded=closed,
                    )
                else:
                    moved = await namespace.move(
                        NodeId(stray.id), NodeId(home.id), if_match=stray.etag, conflict="rename"
                    )
                    if isinstance(moved, OperationState):
                        pending = OperationId(moved.id)
                        log.info(
                            "files.home.adopt_queued", node_id=str(stray.id), op_id=str(pending)
                        )
        except FilesError as refused:
            log.warning(
                "files.home.adopt_refused",
                drive_id=str(DriveId(home.drive_id)),
                node_id=str(stray.id),
                code=refused.code,
            )
            continue
        if pending is not None and background is not None:
            queue_inline(background, files, "large_move", pending)


async def _merge(
    files: FilesContext,
    home: FileNode,
    extra: FileNode,
    *,
    background: BackgroundTasks | None,
) -> None:
    """Fold a second home of the caller's into their home, leaving no trace of it.

    Every live child of ``extra`` is moved into ``home`` by the library's own
    move — paths rewritten, ACL caches invalidated, history and outbox written
    — under a name that yields to whatever the home already holds, so nothing
    is overwritten and nothing is lost. Then the emptied folder, whose name is
    the address this exists to forget, is purged for good; if it still holds
    rows (a child in the trash, a child whose move was too large to run inline
    and was queued), it is renamed to its own id and left for the next read,
    which finds it by the same owner grant and finishes the job.

    One transaction, under the drive lock every Files mutation takes first and
    the ensure's inserts wait behind: two visits racing the merge run it one
    after the other, and the second finds the folder already gone.
    """
    namespace = Namespace(files.repo, files.ctx, files.clock, files.store)
    pending: list[OperationId] = []
    try:
        async with files.repo.transaction():
            await files.repo.lock_chain(
                DriveId(extra.drive_id), None, NodeId(extra.id), rewrites_subtree=True
            )
            current = await files.repo.node(NodeId(extra.id))
            if current is None or current.trashed_at is not None:
                return
            for child in await files.repo.siblings(NodeId(extra.id)):
                moved = await namespace.move(
                    NodeId(child.id), NodeId(home.id), if_match=child.etag, conflict="rename"
                )
                if isinstance(moved, OperationState):
                    pending.append(OperationId(moved.id))
            left = await files.repo.nodes_by_path_prefix(current.path_ids)
            current = await files.repo.node(NodeId(extra.id))
            assert current is not None
            if len(left) == 1 and not pending:
                await Trash(files.repo, files.ctx, files.clock, files.store).purge(NodeId(extra.id))
                log.info("files.home.merged", node_id=str(extra.id), home_id=str(home.id))
            elif bytes(current.name) != str(extra.id).encode("utf-8"):
                await namespace.rename(
                    NodeId(extra.id), str(extra.id).encode("utf-8"), if_match=current.etag
                )
                log.info("files.home.merge_pending", node_id=str(extra.id), left=len(left))
    except FilesError as refused:
        log.warning(
            "files.home.merge_refused",
            node_id=str(extra.id),
            code=refused.code,
        )
        return
    if background is not None:
        for op_id in pending:
            queue_inline(background, files, "large_move", op_id)


def _abandoned(op: FileOp, now: datetime) -> bool:
    """Is this a queued operation no runner ever claimed, old enough to re-hand?

    ``queued`` with no heartbeat is the state a row is born in; only its age
    says whether a runner is about to take it or never will. A running
    operation is somebody's — its stale heartbeat is the watchdog's business.
    """
    return (
        op.state == "queued"
        and op.heartbeat_at is None
        and op.created_at <= now - QUEUED_STALE_AFTER
    )


__all__ = ["caller_home", "home_place"]
