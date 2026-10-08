"""The operations family: observe one, cancel it, undo it, download a tree.

Every handler here is the same four lines — resolve the principal's context,
load through the library, decide through ``enforce``, render — because the
interesting decisions all live below the route. What is worth reading is the
shape of the *loads*: an operation is always fetched and always authorized,
even when it does not exist, so that a caller cannot tell an id that was never
minted from one minted in another org from one they simply may not read. The
sentinel-then-authorize dance in :func:`_resolve` is what buys that: three
classes, one code path, the same statements in the same order.

``download`` is the family's odd member. It does not move the tree, so it has
no inverse; what it produces is a *virtual* archive version — an operation
whose ``resultUrl`` is a signed path on the content domain that streams the
subtree as ZIP64 when redeemed. A subtree small enough to enumerate is walked
now, so the caller learns immediately what will be skipped, by id, in
``errors[]``; a larger one is answered with its size and enumerates itself into
the archive instead. Either way the bytes happen at redemption, and never
through memory.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from alkera_core.config import get_settings
from alkera_core.db.session import get_db
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Enforcer, authorize
from alkera_core.files.authz.decider import AccessFacts
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.ops import (
    AttrsInverse,
    MoveInverse,
    Operations,
    OperationState,
    RawInverse,
    RenameInverse,
    RestoreInverse,
    TrashInverse,
)
from alkera_core.files.uploads import session_of_operation, upload_parent
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    as_platform,
    files_enforcer,
    idempotent_route,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.routes.files.content import fence
from backend.services.files.archive import (
    SubtreeArchive,
    mint_archive_token,
    subtree_archive,
)
from backend.services.files.context import FilesContext
from backend.services.files.guards import refuse_a_copy_over_hidden_chats
from backend.services.files.operations_runner import queue_inline

router = APIRouter(tags=["files"])


def archive_url_ttl() -> timedelta:
    """How long a download's archive link stays redeemable.

    Long enough for a browser to start the transfer, short enough that a link
    pasted into a chat is worthless by the time anyone reads it. Read from the
    deployment settings when the link is minted, so raising
    ``FILES_ARCHIVE_URL_TTL_SECONDS`` moves the next link's deadline.
    """
    return timedelta(seconds=get_settings().files_archive_url_ttl_seconds)


# ---- the wire shape ------------------------------------------------------


class OperationErrorWire(BaseModel):
    """One item the operation could not do, by id and code — never by name."""

    item_id: str | None = Field(default=None, serialization_alias="itemId")
    code: str = ""
    message: str = ""


class OperationConflictWire(BaseModel):
    """A collision the operation resolved, as the pair of node ids involved."""

    from_id: str | None = Field(default=None, serialization_alias="from")
    to_id: str | None = Field(default=None, serialization_alias="to")


class OperationWire(BaseModel):
    """The ``Operation`` resource every client polls."""

    id: str
    drive_id: str = Field(serialization_alias="driveId")
    kind: str
    state: str
    done: int = 0
    total: int | None = None
    bytes: int = 0
    skipped: int = 0
    conflicts: list[OperationConflictWire] = Field(default_factory=list)
    errors: list[OperationErrorWire] = Field(default_factory=list)
    result_node_id: str | None = Field(default=None, serialization_alias="resultNodeId")
    result_version_id: str | None = Field(default=None, serialization_alias="resultVersionId")
    result_unchanged: bool = Field(default=False, serialization_alias="resultUnchanged")
    result_url: str | None = Field(default=None, serialization_alias="resultUrl")
    result_url_expires_at: datetime | None = Field(
        default=None, serialization_alias="resultUrlExpiresAt"
    )
    cancel_requested: bool = Field(default=False, serialization_alias="cancelRequested")
    undoable_until: datetime | None = Field(default=None, serialization_alias="undoableUntil")
    #: Whether this operation records an inverse the undo route can apply. Not
    #: every kind does — a copy creates rather than changes — and a client that
    #: guessed from the kind offered an undo that did nothing when it was
    #: pressed. ``undoableUntil`` answers *how long*, never *whether*.
    undoable: bool = False


def _error_wire(raw: Any) -> OperationErrorWire:
    """One stored error onto the wire. A bare string is the watchdog's."""
    if isinstance(raw, Mapping):
        return OperationErrorWire(
            item_id=_str_or_none(raw.get("itemId")),
            code=str(raw.get("code", "")),
            message=str(raw.get("message", "")),
        )
    return OperationErrorWire(code=str(raw), message=str(raw))


def _conflict_wire(raw: Any) -> OperationConflictWire:
    if isinstance(raw, Mapping):
        return OperationConflictWire(
            from_id=_str_or_none(raw.get("from")), to_id=_str_or_none(raw.get("to"))
        )
    return OperationConflictWire()


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def to_wire(
    state: OperationState,
    *,
    result_url: str | None = None,
    result_url_expires_at: datetime | None = None,
) -> OperationWire:
    """Render an operation. ``resultUrl`` is passed in rather than stored on the
    state because it is minted per read: a link handed out an hour ago must not
    come back alive from a poll.

    ``resultNodeId`` / ``resultVersionId`` are what make the poll an answer
    rather than a progress bar: a queued commit is answered 202 before anything
    exists, so the node it created can only be named here. Only those two facts
    and ``resultUnchanged`` are taken off the stored ``result`` — the rest of
    that bag is the body's own bookkeeping and stays off the wire."""
    return OperationWire(
        id=str(state.id),
        drive_id=str(state.drive_id),
        kind=state.kind,
        state=state.state,
        done=state.done,
        total=state.total,
        bytes=state.bytes,
        skipped=len(state.errors),
        conflicts=[_conflict_wire(row) for row in state.conflicts],
        errors=[_error_wire(row) for row in state.errors],
        result_node_id=_str_or_none(state.result_node_id),
        result_version_id=_str_or_none(state.result.get("version_id")),
        result_unchanged=state.result.get("unchanged") is True,
        result_url=result_url,
        result_url_expires_at=result_url_expires_at,
        cancel_requested=state.cancel_requested,
        undoable_until=state.undoable_until,
        undoable=_undoable(state),
    )


def _undoable(state: OperationState) -> bool:
    """Whether this operation records an inverse this build can still apply.

    The three facts the undo itself checks before it runs: an inverse was
    recorded, this build knows how to apply it, and the window has not closed. A
    client asks here rather than inferring it from the kind, which is how an undo
    came to be offered for a copy — an operation that records no inverse at
    all."""
    if state.inverse is None or isinstance(state.inverse, RawInverse):
        return False
    if state.undoable_until is None:
        return True
    return datetime.now(UTC) <= state.undoable_until


def queued_answer(drive_id: Any, state: OperationState) -> Response:
    """A mutation too large to run inline answers 202 with the operation.

    The tree has not changed yet, so answering with the node would say it had.
    One helper rather than one per family: the queued move, the queued restore
    and anything that later grows a queued branch have to hand back the *same*
    ``Operation`` body and the same ``Location``, or a client would need a
    second progress path per route that can go slow.
    """
    from backend.api.routes.files import PREFIX

    return Response(
        content=to_wire(state).model_dump_json(by_alias=True),
        status_code=202,
        media_type="application/json",
        headers={"Location": f"{PREFIX}/drives/{drive_id}/operations/{state.id}"},
    )


# ---- the pieces every handler shares -------------------------------------


async def access_facts(request: Request, db: AsyncSession, files: FilesContext) -> AccessFacts:
    """This family's name for the shared builder, bound to its own drive.

    Read as the platform, from inside the Files transaction: the mutating
    routes run their whole body under the Files role, which cannot read the
    membership tables the resolver walks.
    """
    async with as_platform(db):
        return await facts_for(request, db, files.ctx, files.drive, now=files.clock.now())


def enforcer(request: Request, db: AsyncSession) -> Enforcer:
    """The shared binding, under this family's historical name, stepping out of
    the tenant role for the engine's statements. The translation it performs is
    spelled once, in ``backend.api.deps.files``."""
    return files_enforcer(request, db, wrap=platform_wrap(db))


def _require_drive(files: FilesContext, drive_id: uuid.UUID) -> DriveId:
    """A drive id that is not this org's drive is simply not there."""
    if files.drive.id != drive_id:
        raise NotFound()
    return DriveId(files.drive.id)


@dataclass(frozen=True, slots=True)
class _UndoSubject:
    """What an undo does: ``action`` on ``node``, and for a move back, a write
    into the folder the node returns to."""

    node: uuid.UUID
    action: FilesAction
    into: uuid.UUID | None = None


async def _undo_subject(files: FilesContext, state: OperationState) -> _UndoSubject | None:
    """What an operation's undo does, decided as the forward work it is.

    Undoing a trash is restoring the trashed root (the decision the restore
    route makes), undoing a restore is trashing the node again, undoing a move
    writes the node back into the folder it came from, and every other inverse
    writes the node it names. ``None`` for an operation with no inverse this
    build can apply; the operation itself refuses that.
    """
    inverse = state.inverse
    if isinstance(inverse, RestoreInverse):
        op = await files.repo.trash_op(inverse.trash_op_id)
        return None if op is None else _UndoSubject(op.root_node_id, FilesAction.RESTORE)
    if isinstance(inverse, TrashInverse):
        return _UndoSubject(inverse.node_id, FilesAction.DELETE)
    if isinstance(inverse, MoveInverse):
        return _UndoSubject(inverse.node_id, FilesAction.WRITE, into=inverse.from_parent_id)
    if isinstance(inverse, RenameInverse | AttrsInverse):
        return _UndoSubject(inverse.node_id, FilesAction.WRITE)
    return None


async def _resolve(
    request: Request,
    db: AsyncSession,
    files: FilesContext,
    operation_id: uuid.UUID,
    action: FilesAction = FilesAction.READ,
    *,
    undo: bool = False,
) -> tuple[OperationState, Authorized[Any]]:
    """Load an operation and authorize the node it concerns, in that order,
    doing the same work whether or not the operation exists.

    The node is the operation's result when it has one, else the node the
    operation concerns — an upload's parent folder, read off the session the
    operation was bound to — so a caller who may read only that folder can
    follow the operation from the moment it is queued: a box on its machine
    credential reads nothing outside the chat folders it holds, and a stand-in
    it may not read would refuse it its own upload until the commit landed.

    The missing case authorizes the drive root instead — the same queries, the
    same policy call, the same decision row — and only then raises the opaque
    404. An operation in another org never comes back from the repo at all, and
    one whose node the caller may not read is refused by the policy: three
    classes, one answer.

    ``undo`` decides the undo itself rather than ``action`` on the operation's
    node (see :func:`_undo_subject`). A trash records no result node, so asking
    the drive root for a write refused every member their own undo.
    """
    ops = Operations(files.repo, files.ctx, files.clock, files.store)
    async with files.repo.transaction():
        facts = await access_facts(request, db, files)
        state: OperationState | None
        try:
            state = await ops.get(OperationId(operation_id))
        except NotFound:
            state = None
        assert files.drive.root_node_id is not None
        target = state.result_node_id if state is not None and state.result_node_id else None
        if target is None and state is not None:
            bound = session_of_operation(state)
            parent = await upload_parent(files.repo, bound) if bound is not None else None
            target = None if parent is None else uuid.UUID(str(parent))
        node_id = NodeId(target if target is not None else files.drive.root_node_id)
        subject = await _undo_subject(files, state) if undo and state is not None else None
        if subject is not None:
            node_id, action = NodeId(subject.node), subject.action
        granted = await authorize(
            files.ctx,
            files.repo,
            node_id,
            action,
            facts=facts,
            enforce=enforcer(request, db),
        )
        if subject is not None and subject.into is not None:
            await authorize(
                files.ctx,
                files.repo,
                NodeId(subject.into),
                FilesAction.WRITE,
                facts=facts,
                enforce=enforcer(request, db),
            )
    if state is None:
        raise NotFound()
    return state, granted


Db = Annotated[AsyncSession, Depends(get_db, scope="function")]


# ---- the routes ----------------------------------------------------------


@router.get(
    "/drives/{drive_id}/operations/{operation_id}",
    response_model=OperationWire,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("operations.poll"))],
)
async def get_operation(
    request: Request,
    db: Db,
    files: FilesCtx,
    drive_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> OperationWire:
    """One operation's state. Any session, so progress is observable from a
    second client while the first is still running the work."""
    _require_drive(files, drive_id)
    state, _ = await _resolve(request, db, files, operation_id)
    url, expires = await _archive_link(files, state)
    return to_wire(state, result_url=url, result_url_expires_at=expires)


@router.post(
    "/drives/{drive_id}/operations/{operation_id}/cancel",
    response_model=OperationWire,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("operations.write"))],
)
@idempotent_route("files.operations.cancel_operation")
async def cancel_operation(
    request: Request,
    db: Db,
    files: FilesCtx,
    drive_id: uuid.UUID,
    operation_id: uuid.UUID,
    _key: Idempotency,
    _if_match: IfMatch,
    _lease: Lease,
) -> OperationWire:
    """Ask an operation to stop.

    A queued operation is cancelled outright; a running one is *asked*, and
    stops at its next batch boundary — which is why the answer to cancelling a
    running operation is a state that still says ``running`` with the request
    recorded, and not a lie about work that is still committing.
    """
    _require_drive(files, drive_id)
    state, _ = await _resolve(request, db, files, operation_id, FilesAction.WRITE)
    ops = Operations(files.repo, files.ctx, files.clock, files.store)
    try:
        cancelled = await ops.cancel(state.id)
    except InvalidRequest as exc:
        raise Conflict("files.operation_settled", str(exc)) from None
    return to_wire(cancelled)


@router.post(
    "/drives/{drive_id}/operations/{operation_id}/undo",
    response_model=OperationWire,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ratelimited("operations.write"))],
)
@idempotent_route("files.operations.undo_operation", status=status.HTTP_202_ACCEPTED)
async def undo_operation(
    request: Request,
    db: Db,
    files: FilesCtx,
    drive_id: uuid.UUID,
    operation_id: uuid.UUID,
    background: BackgroundTasks,
    _key: Idempotency,
    _if_match: IfMatch,
    lease: Lease,
) -> Any:
    """Apply an operation's inverse as a NEW operation.

    The answer is the new operation, not the old one: undo is forward work with
    its own id, its own progress and its own inverse, which is what makes undo
    of an undo a redo without a second code path.

    An inverse that could not run inline — an oversized restore whose re-parent
    is a queued move — is answered with THAT operation instead, through the same
    202-with-``Location`` shape the restore route uses, and its runner is
    started here. Answering with the undo row would report work as done that
    nothing had yet started.
    """
    _require_drive(files, drive_id)
    state, _ = await _resolve(request, db, files, operation_id, FilesAction.WRITE, undo=True)
    ops = Operations(files.repo, files.ctx, files.clock, files.store)
    try:
        undone, queued = await ops.undo(state.id, lease=fence(lease, files))
    except InvalidRequest as exc:
        raise Conflict("files.not_undoable", str(exc)) from None
    if queued is not None:
        queue_inline(background, files, "large_move", OperationId(queued.id))
        return queued_answer(drive_id, queued)
    return to_wire(undone)


@router.post(
    "/drives/{drive_id}/items/{item_id}/download",
    response_model=OperationWire,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ratelimited("download", limit=60))],
)
@idempotent_route("files.operations.download_subtree", status=status.HTTP_202_ACCEPTED)
async def download_subtree(
    request: Request,
    db: Db,
    files: FilesCtx,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    _key: Idempotency,
    _if_match: IfMatch,
    _lease: Lease,
) -> OperationWire:
    """Start a download of a subtree as one ZIP64 archive.

    How much the request itself does depends on how big the subtree is, and the
    size is asked with a count rather than by reading the rows. Under
    ``FILES_DOWNLOAD_INLINE_MAX_NODES`` the walk runs here, a page at a time,
    so the operation comes back with its ``errors[]`` already listing by id
    everything the archive will not contain. Above it the request answers with
    the subtree's size alone: enumerating a hundred thousand omissions into one
    column is not a better answer than the archive's own ``skipped.txt``, which
    is decided fresh at redemption anyway.

    Either way the bytes happen only when the signed ``resultUrl`` is redeemed,
    streamed, and never buffered.
    """
    drive = _require_drive(files, drive_id)
    async with files.repo.transaction():
        facts = await access_facts(request, db, files)
        root = await authorize(
            files.ctx,
            files.repo,
            NodeId(item_id),
            FilesAction.EXPORT,
            facts=facts,
            enforce=enforcer(request, db),
        )

        async def decide(node_id: uuid.UUID, action: FilesAction) -> Authorized[Any]:
            return await authorize(
                files.ctx,
                files.repo,
                NodeId(node_id),
                action,
                facts=facts,
                enforce=enforcer(request, db),
            )

        # The walk below prunes what the caller may not read, but a pruned
        # chat would still be named by id in the archive's own manifest and in
        # this operation's errors. A parent holding a conversation the caller
        # cannot open is refused before the archive exists at all.
        await refuse_a_copy_over_hidden_chats(
            files.repo, files.ctx, root.node, facts=facts, decide=decide, action=FilesAction.EXPORT
        )
        size = await files.repo.count_in_subtree(root.node)
    walked: SubtreeArchive | None = None
    if size <= get_settings().files_download_inline_max_nodes:
        walked = subtree_archive(files, root, facts)
        # The members are counted and dropped: what the caller is told is how
        # many there are and what was left out, and holding the entries to say
        # so would be the tree in memory all over again.
        async for _member in walked.entries():
            pass
    ops = Operations(files.repo, files.ctx, files.clock, files.store)
    total = walked.counted if walked is not None else size
    started = await ops.start("download", drive_id=drive, total=total)
    await _record_download(files, started.id, root.node.id, walked, total=total)
    state = await ops.get(started.id)
    url, expires = await _archive_link(files, state)
    return to_wire(state, result_url=url, result_url_expires_at=expires)


# ---- the download's own bookkeeping --------------------------------------


async def _record_download(
    files: FilesContext,
    op_id: OperationId,
    node_id: uuid.UUID,
    walked: SubtreeArchive | None,
    *,
    total: int,
) -> None:
    """Settle the download onto the operation row: what it will contain, how
    many bytes that is, what it will not, and which node it is rooted at.

    ``walked`` is ``None`` for a subtree too large to have been enumerated in
    the request. The row then carries the subtree's size and no omissions,
    because none have been decided yet — the archive's own ``skipped.txt``
    is what names them, and it is written from the walk that produces the
    bytes.
    """
    from sqlalchemy import text

    skips = walked.skips if walked is not None else ()
    progress: dict[str, Any] = {
        "done": total,
        "total": total,
        "bytes": walked.total_bytes if walked is not None else 0,
        "kind": "download",
        "planned": "inline" if walked is not None else "on_redeem",
    }
    if walked is not None and walked.omitted:
        progress["skippedUnlisted"] = walked.omitted
    async with files.repo.transaction():
        await files.repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'done', result_node_id = :node, "
                "errors = CAST(:errors AS jsonb), "
                "progress = progress || CAST(:progress AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {
                "id": op_id,
                "org": files.repo.scope.org_team_id,
                "node": node_id,
                "errors": _dumps([skip.as_error() for skip in skips]),
                "progress": _dumps(progress),
            },
        )


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value)


async def _archive_link(
    files: FilesContext, state: OperationState
) -> tuple[str | None, datetime | None]:
    """The signed content path for a finished download, minted per read."""
    if state.kind != "download" or state.state != "done" or state.result_node_id is None:
        return None, None
    expires_at = files.clock.now() + archive_url_ttl()
    token = mint_archive_token(
        files, operation_id=state.id, node_id=state.result_node_id, expires_at=expires_at
    )
    base = files.settings.files_content_base_url or ""
    return f"{base}/c/archive/{token}", expires_at


__all__ = [
    "OperationConflictWire",
    "OperationErrorWire",
    "OperationWire",
    "access_facts",
    "archive_url_ttl",
    "enforcer",
    "queued_answer",
    "router",
    "to_wire",
]
