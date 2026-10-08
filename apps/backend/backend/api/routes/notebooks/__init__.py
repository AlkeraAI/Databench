"""The notebook routes: ``/api/v1/notebooks/{drive_id}/{item_id}/...``.

Thin: each route admits the caller through the Files policy (and, for what
acts through the kernel, the ``notebook.run`` policy) and hands the rest to
:class:`~backend.services.notebooks.service.NotebookService`. See that module
for who may do what.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import structlog
from alkera_core.authz import Action, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.db.session import get_db
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.objects_bridge import CHAT_TYPE
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.schemas import (
    CELL_ID_PATTERN,
    FRAME_ID_PATTERN,
    SHA256_PATTERN,
    Accepted,
    Activity,
    CommRequest,
    EnvChangeAction,
    EnvChangeRequest,
    EnvInstallRequest,
    FrameAttached,
    FrameAttachRequest,
    KernelRequest,
    NotebookConnections,
    NotebookEditor,
    NotebookOpsRequest,
    OpErrorBody,
    OutputsClearRequest,
    RebaseRequest,
    RebaseResult,
    RunAccepted,
    RunRequest,
    TablePage,
    WidgetAssetResolved,
)
from alkera_core.observability.envelope import ErrorEnvelope
from alkera_core.schemas.realtime.machine import NOTEBOOK_FOLDER_NOT_HELD
from alkera_notebook.engine.models import (
    EnvListing,
    EnvPackages,
    KernelInfo,
    NotebookOpsResult,
    NotebookView,
    StoredNotebook,
)
from alkera_notebook.engine.sort_spec import parse_sort_spec
from alkera_notebook.outputs import RASTER_MIMES
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.chat_wake import first_sendable, reader_of, wake_first_sendable
from backend.api.deps.files import Lease, caller_drive, ratelimited
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_errors import refusal_body
from backend.api.deps.notebooks import (
    admit,
    authorized,
    caller_of,
    fresh_bytes,
    mint_download_url,
)
from backend.api.params import ID_PATTERN, PathId, strict_path_id
from backend.authz import decide_on_record, enforce
from backend.services import sharing, workspaces
from backend.services.connections import member_connections
from backend.services.crdt import CrdtError
from backend.services.files import NotEditableError, folder_holder
from backend.services.notebooks import (
    CATALOG_FACTS,
    MAX_BATCH_EVENTS,
    MAX_EVENT_SEQ,
    AgentChatRefusedError,
    Caller,
    KernelAnswerError,
    KernelRefusedError,
    KernelSilentError,
    NoMachineError,
    NotebookService,
    NotebookUnavailableError,
    OpRefusedError,
    Target,
    accept_events,
    agent_of_holder,
    connection_choices,
    follow_runs,
    holder_on,
    is_waking,
    notebook_chats,
    read_stored,
    runner_of,
    saved_image,
    seq_out_of_range,
    service_for,
    snapshot_node,
    waits_on_wake,
    workspace_and_owner,
)

PREFIX: Final = "/api/v1/notebooks"
#: How long a caller waits before trying again a request the box did not answer.
RETRY_SILENT_SECONDS: Final = 2
#: The live document's refusals the same request clears once it is tried again.
RETRYABLE_CRDT_CODES: Final = frozenset({"crdt_busy", "content_landing"})
#: What a request that needs the notebook's machine is answered with while the
#: chat holding its folder wakes: the same request is asked again after
#: :data:`RETRY_WAKING_SECONDS`.
WAKING_CODE: Final = "notebook.waking"
WAKING_MESSAGE: Final = "Waking the machine…"
RETRY_WAKING_SECONDS: Final = 3
#: What a request is refused with when the workspace's machine is gone, so
#: waking its chat would start nothing.
MACHINE_GONE_MESSAGE: Final = (
    "This workspace's machine is gone. Open the workspace to pick another."
)
#: The kernel actions that start a kernel, and so need the notebook's machine.
KERNEL_STARTS: Final = frozenset({"restart"})
#: What a route that may wake the notebook's chat answers besides its result.
_WAKES: Final[dict[int | str, dict[str, Any]]] = {
    409: {"model": ErrorEnvelope},
    503: {"model": ErrorEnvelope},
}

log = structlog.get_logger(__name__)

router = APIRouter(prefix=PREFIX, tags=["notebooks"])

Db = Annotated[AsyncSession, Depends(get_db, scope="function")]
#: Every route names the drive first: a stranger's request is the opaque 404
#: every Files route answers, before the body is read.
_DRIVE = [Depends(caller_drive)]


class KernelResult(BaseModel):
    """The kernel as the platform last heard of it (``state`` is ``absent``
    when it has none), and whether the action was handed on."""

    kernel: KernelInfo
    accepted: bool


class SortKey(BaseModel):
    column: str
    descending: bool = False


def parse_sort(raw: str | None) -> list[SortKey]:
    """A table page's ``sort`` parameter, ``col:asc,col2:desc`` (the direction
    defaults to ascending), as sort keys, read by the engine's one reader of
    that syntax. Raises :class:`ValueError` for one that does not read: an
    empty column, a direction other than ``asc`` or ``desc``, or more keys
    than the engine sorts by. Whether each column exists is the frame's to
    say (the kernel refuses an unknown one)."""
    return [SortKey.model_validate(key) for key in parse_sort_spec(raw)]


class KernelEventsBatch(BaseModel):
    """Box -> backend: a batch of a kernel's events, in sequence order."""

    model_config = ConfigDict(extra="forbid")

    kernel_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    state: str | None = Field(default=None, max_length=16)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_BATCH_EVENTS)


class KernelEventsAccepted(BaseModel):
    kernel_id: str
    accepted: int
    seq: int


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=refusal_body(status=status, code=code, message=message)
    )


def _service(request: Request) -> NotebookService:
    return service_for(request.app, decide=decide_on_record)


def _refusal(exc: Exception) -> JSONResponse | None:
    """A service refusal as its HTTP answer; ``None`` for anything else."""
    if isinstance(exc, OpRefusedError):
        flat = OpErrorBody(
            code=exc.code,  # type: ignore[arg-type]  # the lane names only OpErrorCode values
            message=exc.message,
            op_index=exc.op_index,
        ).model_dump()
        return JSONResponse(
            status_code=422,
            content=refusal_body(
                status=422,
                code=exc.code,
                message=exc.message,
                detail={"op_index": exc.op_index},
                flat=flat,
            ),
        )
    if isinstance(exc, NoMachineError):
        return _error(409, "notebook.no_machine", str(exc))
    if isinstance(exc, NotebookUnavailableError):
        return _error(503, "notebook.unavailable", str(exc))
    if isinstance(exc, KernelAnswerError):
        if exc.code == NOTEBOOK_FOLDER_NOT_HELD:
            # The box does not hold the folder its lease names, and this
            # caller woke nothing: the same request clears once it does.
            waiting = _error(503, exc.code, exc.message)
            waiting.headers["Retry-After"] = str(RETRY_SILENT_SECONDS)
            return waiting
        return _error(409, exc.code, exc.message)
    if isinstance(exc, KernelSilentError):
        # The box did not answer though the request was sent again while it
        # waited (a box whose worker restarts is back within seconds): the
        # same request may be tried again.
        silent = _error(503, "notebook.kernel_silent", str(exc))
        silent.headers["Retry-After"] = str(RETRY_SILENT_SECONDS)
        return silent
    if isinstance(exc, CrdtError):
        return _crdt_refusal(exc)
    return None


def _crdt_refusal(exc: CrdtError) -> JSONResponse:
    """A live document's refusal as its answer. One the same request clears
    later (a busy lane, a file whose bytes are still landing) is a 503 with
    the wait; one about the file itself is a 409 whose message names why."""
    if exc.code == "not_found":
        raise NotFound() from exc
    log.info("notebook.refused", code=exc.code, reason=exc.reason)
    if exc.code in RETRYABLE_CRDT_CODES:
        answer = _error(503, exc.code, exc.message)
        if exc.retry_after_ms is not None:
            answer.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_ms / 1000)))
        return answer
    if exc.code == "crdt_unsupported":
        return _error(503, exc.code, exc.message)
    message = f"{exc.message} ({exc.reason})" if exc.reason else exc.message
    return _error(409, exc.code, message)


async def _wake_first(
    request: Request,
    db: AsyncSession,
    files: FilesCtx,
    target: Target,
    *,
    run_decided: bool,
    box_refused: bool = False,
) -> JSONResponse | None:
    """Wake the chat the notebook's folder waits on, the way opening that
    chat wakes it, and answer ``notebook.waking`` (``503`` with the wait) for
    the caller to ask again once the box holds the folder.

    The folder waits on a wake when every chat working there sleeps and
    nothing else holds it (:func:`waits_on_wake`), or when the box it was sent
    to said it does not hold it (``box_refused``). Only a caller the
    ``notebook.run`` policy lets run the notebook wakes it (``run_decided``
    when the route enforced that already), and only through a chat they may
    send in. The answer is ``notebook.waking`` only while a box is coming for
    the chat (or the box just said it does not hold the folder), never for a
    chat no box will open. ``None`` when nothing waits or this caller may wake
    nothing: the request then goes on and is answered as it always was."""
    ctx = files.ctx
    if ctx.is_machine:
        return None
    chats = await notebook_chats(db, target)
    if not chats:
        return None
    if not box_refused:
        scope = target.scope
        holder = scope.holder if scope is not None else await folder_holder(db, ctx, target.item_id)
        if not await waits_on_wake(db, org_id=target.org_id, chats=chats, holder=holder):
            return None
    caller = await runner_of(db, files)
    if caller.user is None:
        return None
    if not run_decided:
        resource, attrs, target = await _service(request).run_facts(db, files, target)
        if not decide_policy(ctx, Action.RUN, resource, attrs).allowed:
            return None
    reader = await reader_of(request, db, ctx, caller.user)
    woke = await wake_first_sendable(request, db, ctx, chats, user=caller.user, reader=reader)
    if woke is None:
        return None
    chat, answer = woke
    if answer.outcome == "machine_unavailable":
        return _error(409, "notebook.machine_unavailable", MACHINE_GONE_MESSAGE)
    if not box_refused and not await is_waking(db, chat):
        return None
    waking = _error(503, WAKING_CODE, WAKING_MESSAGE)
    waking.headers["Retry-After"] = str(RETRY_WAKING_SECONDS)
    return waking


async def _ask_refused(
    request: Request, db: AsyncSession, files: FilesCtx, target: Target, exc: Exception
) -> JSONResponse | None:
    """A refused request the engine answers as its answer: the box saying it
    does not hold the notebook's folder wakes the folder's chat first."""
    if isinstance(exc, KernelAnswerError) and exc.code == NOTEBOOK_FOLDER_NOT_HELD:
        waking = await _wake_first(request, db, files, target, run_decided=False, box_refused=True)
        if waking is not None:
            return waking
    return _refusal(exc)


@router.post(
    "/{drive_id}/{item_id}/ops",
    response_model=NotebookOpsResult,
    dependencies=[*_DRIVE, Depends(ratelimited("notebook_ops"))],
    responses={
        422: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        503: {"model": ErrorEnvelope},
    },
)
async def apply_ops(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    lease: Lease,
    body: NotebookOpsRequest,
) -> Any:
    """Apply a batch of operations on the notebook's live document, as the
    agent (through its person's session) or the box holding the folder (under
    its lease's fence). Idempotent by ``submit_id``."""
    target = await admit(request, db, files, item_id, FilesAction.WRITE)
    caller = await caller_of(db, files, target, lease)
    if body.agent_chat_id is not None:
        try:
            caller = await agent_of_holder(db, target, caller, body.agent_chat_id)
        except AgentChatRefusedError as exc:
            await db.rollback()
            return _error(403, "notebook.agent_chat_refused", str(exc))
    try:
        return await _service(request).apply_ops(
            db,
            target,
            caller,
            ops=[op.model_dump(mode="json") for op in body.ops],
            base_token=body.base_token,
            submit_id=body.submit_id,
        )
    except (OpRefusedError, NotebookUnavailableError, CrdtError) as exc:
        refused = _refusal(exc)
        if refused is None:  # pragma: no cover - every caught type maps
            raise
        return refused


@router.post(
    "/{drive_id}/{item_id}/rebase",
    response_model=RebaseResult,
    dependencies=[*_DRIVE, Depends(ratelimited("notebook_ops"))],
    responses={409: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)
async def rebase_update(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    lease: Lease,
    body: RebaseRequest,
) -> Any:
    """Hand over an editor's typing that never reached the document because
    its history restarted first: carried into the current epoch cell by
    cell. Files WRITE, as for any edit."""
    target = await admit(request, db, files, item_id, FilesAction.WRITE)
    caller = await caller_of(db, files, target, lease)
    try:
        update = base64.b64decode(body.update, validate=True)
    except (binascii.Error, ValueError):
        return _error(422, "notebook.bad_update", "the update is not base64")
    try:
        return await _service(request).rebase(db, target, caller, epoch=body.epoch, update=update)
    except (NotebookUnavailableError, CrdtError) as exc:
        refused = _refusal(exc)
        if refused is None:  # pragma: no cover - every caught type maps
            raise
        return refused


@router.get(
    "/{drive_id}/{item_id}",
    response_model=NotebookView,
    dependencies=_DRIVE,
    responses={503: {"model": ErrorEnvelope}},
)
async def read_notebook(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db
) -> Any:
    """The live document, the kernel's state and who is in which cell."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    try:
        return await _service(request).view(db, target)
    except (NotebookUnavailableError, CrdtError) as exc:
        refused = _refusal(exc)
        if refused is None:  # pragma: no cover
            raise
        return refused


@router.get(
    "/{drive_id}/{item_id}/connections",
    response_model=NotebookConnections,
    dependencies=_DRIVE,
)
async def notebook_connections(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db
) -> NotebookConnections:
    """The connections the notebook's SQL cells may name: the connections of
    the workspace that holds it, the set its kernel resolves a name among on
    the box (the workspace owner's, shared with everyone the workspace is
    shared with). Files READ on the notebook; each entry says whether this
    reader can use it, and why not. Empty for a notebook in no workspace."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    held = await workspace_and_owner(db, target)
    if held is None:
        return NotebookConnections()
    workspace, owner = held
    resource, attrs, _bound = await _service(request).run_facts(db, files, target)
    may_run = decide_policy(files.ctx, Action.RUN, resource, attrs)
    listed = await member_connections(db, owner, org_id=workspace.org_team_id)
    await db.commit()
    return connection_choices(
        listed.connections,
        owner=owner,
        facts=CATALOG_FACTS,
        run_refusal=None if may_run.allowed else f"{may_run.message.rstrip('.')}.",
    )


@router.get("/{drive_id}/{item_id}/editor", response_model=NotebookEditor, dependencies=_DRIVE)
async def notebook_editor(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db
) -> NotebookEditor:
    """Where the notebook editor opens the notebook ``item_id``, or a new
    notebook in the folder ``item_id``: the chat whose workspace pane runs it
    (the chat the same wake a run asks for goes through), else nowhere. Files
    READ on the node; the chat is named only to a caller its policy lets send
    in it, since the editor is opened to edit and run."""
    try:
        node_id = NodeId(uuid.UUID(item_id))
    except ValueError:
        raise NotFound() from None
    allowed = await authorized(request, db, files, node_id, FilesAction.READ)
    holding = await holder_on(db, [*allowed.chain, allowed.node], files.repo.scope.org_team_id)
    caller = await runner_of(db, files)
    user = caller.user
    if holding is None or user is None:
        return NotebookEditor()
    ctx = files.ctx
    reader = await reader_of(request, db, ctx, user)
    if holding.type != CHAT_TYPE:
        recent = await workspaces.recent_chats(db, holding.id)
        picked = await first_sendable(db, ctx, recent, reader=reader)
        return NotebookEditor(chat_id=None if picked is None else str(picked[0].id))
    attrs = await sharing.chat_attrs(db, holding, reader)
    resource = sharing.object_resource(holding, type=ResourceType.CHAT)
    if not decide_policy(ctx, Action.SEND, resource, attrs).allowed:
        return NotebookEditor()
    return NotebookEditor(chat_id=str(holding.id))


async def _exportable_snapshot(
    request: Request, db: AsyncSession, files: FilesCtx, target: Target
) -> FileNode | None:
    """The saved snapshot beside the notebook, when there is one and the
    caller may EXPORT it, as they may the file's own bytes."""
    snapshot = await snapshot_node(files, target)
    if snapshot is None:
        return None
    try:
        await authorized(
            request, db, files, NodeId(uuid.UUID(str(snapshot.id))), FilesAction.EXPORT
        )
    except FilesError:
        return None
    return snapshot


#: What a reader is told when the notebook's file cannot be read as text.
_UNREADABLE: Final = {
    "gone": "This notebook is no longer available.",
    "too_large": "This notebook is too large to show as a notebook.",
    "binary": "This file can't be read as a notebook.",
}


@router.get(
    "/{drive_id}/{item_id}/stored",
    response_model=StoredNotebook,
    dependencies=_DRIVE,
    responses={409: {"model": ErrorEnvelope}},
)
async def read_stored_notebook(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db
) -> Any:
    """The notebook as the drive stores it: its cells and the outputs saved
    beside it. Decided for EXPORT, as the file's own bytes are, and read with
    no live document and no kernel: what a preview of the file shows."""
    target = await admit(request, db, files, item_id, FilesAction.EXPORT)
    snapshot = await _exportable_snapshot(request, db, files, target)
    try:
        return await read_stored(db, files.ctx, target, snapshot)
    except NotEditableError as exc:
        message = _UNREADABLE.get(exc.reason, _UNREADABLE["binary"])
        return _error(409, f"notebook_{exc.reason}", message)


async def _kernel_target(
    request: Request, db: AsyncSession, files: FilesCtx, item_id: PathId
) -> tuple[Target, Caller]:
    """The notebook and the caller, once ``notebook.run`` allowed them."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    caller = await runner_of(db, files)
    resource, attrs, target = await _service(request).run_facts(db, files, target)
    await enforce(request, db, files.ctx, Action.RUN, resource, attrs)
    return target, caller


async def _env_target(
    request: Request, db: AsyncSession, files: FilesCtx, item_id: str
) -> tuple[Target, Caller]:
    """:func:`_kernel_target`, with whether the caller may also edit the
    notebook riding the request: an environment action on the notebook's own
    PEP 723 block is an edit, and the box acts on exactly this verdict."""
    target, caller = await _kernel_target(request, db, files, item_id)
    may_edit = target.allowed.access.allows(FilesAction.WRITE)
    return target, dataclasses.replace(caller, may_edit=may_edit)


@router.post(
    "/{drive_id}/{item_id}/runs",
    response_model=RunAccepted,
    dependencies=[*_DRIVE, Depends(ratelimited("notebook_runs"))],
    responses={409: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope}},
)
async def run_notebook(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db, body: RunRequest
) -> Any:
    """Run cells: their text is taken at the requester's frontier (waiting up
    to 2 s for the document to include it), the run is recorded and handed to
    the kernel. Idempotent by ``client_run_id``. A notebook whose chat sleeps
    wakes it first and is answered ``notebook.waking``; nothing is recorded,
    and the same request is asked again."""
    target, caller = await _kernel_target(request, db, files, item_id)
    waking = await _wake_first(request, db, files, target, run_decided=True)
    if waking is not None:
        return waking
    try:
        return await _service(request).run(db, files, target, caller, body)
    except (NoMachineError, NotebookUnavailableError, CrdtError) as exc:
        refused = _refusal(exc)
        if refused is None:  # pragma: no cover
            raise
        return refused


@router.post(
    "/{drive_id}/{item_id}/kernel",
    response_model=KernelResult,
    dependencies=_DRIVE,
    responses=_WAKES,
)
async def kernel_action(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    body: KernelRequest,
) -> Any:
    """The kernel's state (``status``, Files READ), or an interrupt, restart
    or shutdown carried to it (``notebook.run``). A restart, which starts a
    kernel, wakes a sleeping chat first; stopping a kernel never does (a
    sleeping chat runs none)."""
    service = _service(request)
    if body.action == "status":
        target = await admit(request, db, files, item_id, FilesAction.READ)
        return KernelResult(kernel=await service.kernel_state(db, target), accepted=True)
    target, caller = await _kernel_target(request, db, files, item_id)
    if body.action in KERNEL_STARTS:
        waking = await _wake_first(request, db, files, target, run_decided=True)
        if waking is not None:
            return waking
    try:
        await service.to_kernel(db, files, target, caller, "kernel", {"action": body.action})
    except NoMachineError as exc:
        return _refusal(exc)
    return KernelResult(kernel=await service.kernel_state(db, target), accepted=True)


@router.post(
    "/{drive_id}/{item_id}/outputs/clear",
    response_model=Accepted,
    dependencies=_DRIVE,
    responses=_WAKES,
)
async def clear_outputs(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    body: OutputsClearRequest,
) -> Any:
    """Clear cells' outputs for everyone (every cell's without ``cell_ids``):
    the engine drops them from every view and from the saved outputs. Decided
    by ``notebook.run``, as running the cells that made them was."""
    target, caller = await _kernel_target(request, db, files, item_id)
    waking = await _wake_first(request, db, files, target, run_decided=True)
    if waking is not None:
        return waking
    try:
        request_id = await _service(request).to_kernel(
            db, files, target, caller, "outputs_clear", {"cell_ids": body.cell_ids}
        )
    except NoMachineError as exc:
        return _refusal(exc)
    return Accepted(accepted=True, request_id=str(request_id))


@router.post(
    "/{drive_id}/{item_id}/comm",
    response_model=Accepted,
    dependencies=[*_DRIVE, Depends(ratelimited("notebook_comm"))],
    responses={409: {"model": ErrorEnvelope}},
)
async def send_comm(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db, body: CommRequest
) -> Any:
    """A person's widget message from one of their attached output frames
    (the engine's hub refuses a comm the frame does not own)."""
    target, caller = await _kernel_target(request, db, files, item_id)
    if _service(request).frame_of(target, caller, body.frame_id) is None:
        raise NotFound()
    try:
        request_id = await _service(request).to_kernel(
            db, files, target, caller, "comm", body.model_dump(mode="json")
        )
    except NoMachineError as exc:
        return _refusal(exc)
    return Accepted(accepted=True, request_id=str(request_id))


@router.post(
    "/{drive_id}/{item_id}/env/install",
    response_model=Accepted,
    dependencies=_DRIVE,
    responses=_WAKES,
)
async def install_packages(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    body: EnvInstallRequest,
) -> Any:
    """Install packages into the notebook's environment, through its kernel.
    The outcome is announced on the notebook's channel (``env.install``)."""
    target, caller = await _env_target(request, db, files, item_id)
    waking = await _wake_first(request, db, files, target, run_decided=True)
    if waking is not None:
        return waking
    try:
        request_id = await _service(request).env_action(
            db, files, target, caller, "install", body.packages
        )
    except NoMachineError as exc:
        return _refusal(exc)
    return Accepted(accepted=True, request_id=str(request_id))


@router.post(
    "/{drive_id}/{item_id}/env/{action}",
    response_model=Accepted,
    dependencies=_DRIVE,
    responses={**_WAKES, 422: {"model": ErrorEnvelope}},
)
async def change_environment(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    action: EnvChangeAction,
    files: FilesCtx,
    db: Db,
    body: EnvChangeRequest,
) -> Any:
    """Build the notebook's environment from its spec now, remove
    packages from it, or cancel the build under way, through its kernel. The
    outcome is announced on the notebook's channel (``env.install``, naming
    the ``action``)."""
    target, caller = await _env_target(request, db, files, item_id)
    if action == "remove" and not body.packages:
        return _error(422, "notebook.packages_required", "Name the packages to remove.")
    waking = await _wake_first(request, db, files, target, run_decided=True)
    if waking is not None:
        return waking
    try:
        request_id = await _service(request).env_action(
            db, files, target, caller, action, body.packages
        )
    except NoMachineError as exc:
        return _refusal(exc)
    return Accepted(accepted=True, request_id=str(request_id))


@router.get("/{drive_id}/{item_id}/activity", response_model=Activity, dependencies=_DRIVE)
async def notebook_activity(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    since: Annotated[datetime, Query()],
    exclude_actor: Annotated[str | None, Query(max_length=160)] = None,
) -> Any:
    """Edits and runs since ``since``, oldest first."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    return await _service(request).activity(db, target, since=since, exclude_actor=exclude_actor)


#: How long a browser may keep an inline image it was served: the bytes are
#: named by their hash, so they never change under the URL.
INLINE_IMAGE_CACHE: Final = "private, max-age=86400, immutable"


@router.get(
    "/{drive_id}/{item_id}/blobs/{sha256}",
    status_code=302,
    response_class=RedirectResponse,
    dependencies=_DRIVE,
    responses={
        200: {
            "description": "A raster image the notebook carries inline, as its bytes.",
            "content": {mime: {} for mime in sorted(RASTER_MIMES)},
        },
        404: {"model": ErrorEnvelope},
    },
)
async def output_blob(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    sha256: Annotated[str, strict_path_id(SHA256_PATTERN)],
) -> Response:
    """An output stored beside the notebook, by its hash: a redirect to its
    bytes, fetched from the machine holding the folder first when the drive
    does not have them yet. A raster image small enough to be carried inline
    has no file of its own; it is answered as its bytes, found among the
    outputs the cells show now (under the same READ as the notebook's view)
    or, failing that, in the saved snapshot (under the same EXPORT as the
    stored notebook). An output no cell holds any more is a 404."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    service = _service(request)
    node = await service.blob_node(files, target, sha256)
    if node is not None:
        return await _serve(request, db, files, node.id)
    image = service.live_image(target, sha256)
    if image is None:
        snapshot = await _exportable_snapshot(request, db, files, target)
        image = None if snapshot is None else await saved_image(db, files.ctx, snapshot, sha256)
    if image is None:
        raise NotFound()
    return Response(
        content=image.data,
        media_type=image.mime,
        headers={"Cache-Control": INLINE_IMAGE_CACHE},
    )


_ANSWERED: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorEnvelope},
    409: {"model": ErrorEnvelope},
    503: {"model": ErrorEnvelope},
}
_KERNEL_ERRORS = (NoMachineError, KernelAnswerError, KernelSilentError, NotebookUnavailableError)


@router.post(
    "/{drive_id}/{item_id}/frames",
    response_model=FrameAttached,
    dependencies=[*_DRIVE, Depends(ratelimited("notebook_comm"))],
    responses=_ANSWERED,
)
async def attach_frame(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    body: FrameAttachRequest,
) -> Any:
    """Attach an output frame at the engine's widget hub: its id and the
    comm-open replays of its models. Later widget events for it arrive on the
    notebook channel addressed by ``frame_id``, to its owner only. A frame is
    where widget messages are sent from, so attaching one is decided by
    ``notebook.run``."""
    target, caller = await _kernel_target(request, db, files, item_id)
    try:
        return await _service(request).attach_frame(db, files, target, caller, body)
    except _KERNEL_ERRORS as exc:
        return _refusal(exc)


@router.delete(
    "/{drive_id}/{item_id}/frames/{frame_id}",
    status_code=204,
    dependencies=_DRIVE,
    responses=_ANSWERED,
)
async def detach_frame(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    frame_id: Annotated[str, strict_path_id(FRAME_ID_PATTERN)],
) -> Response:
    """Detach one of the caller's output frames. Not decided by
    ``notebook.run``: it only removes a frame the signed id names as the
    caller's and reaches no code in the kernel, and a person who lost Can
    edit must still be able to drop the frames they attached before."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    caller = await runner_of(db, files)
    service = _service(request)
    if service.frame_of(target, caller, frame_id) is None:
        raise NotFound()
    try:
        await service.detach_frame(db, files, target, caller, frame_id)
    except NoMachineError:
        # No machine, no hub: the frame is attached nowhere.
        pass
    return Response(status_code=204)


@router.get(
    "/{drive_id}/{item_id}/cells/{cell_id}/table",
    response_model=TablePage,
    dependencies=_DRIVE,
    responses=_ANSWERED,
)
async def table_page(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    cell_id: Annotated[str, strict_path_id(CELL_ID_PATTERN)],
    offset: Annotated[int, Query(ge=0, le=2**53)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    sort: Annotated[
        str | None,
        Query(max_length=4096, description="col:asc,col2:desc (direction defaults to asc)"),
    ] = None,
    filter_sql: Annotated[str | None, Query(max_length=8192)] = None,
) -> Any:
    """A page of a table output, read by the kernel's ``inspect.frame``
    (``filter_sql`` is one ``SELECT`` over a table named ``frame``; the
    kernel refuses anything else)."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    try:
        keys = parse_sort(sort)
    except ValueError as exc:
        return _error(422, "notebook.bad_sort", str(exc))
    query = {
        "cell_id": cell_id,
        "offset": offset,
        "limit": limit,
        "sort": [key.model_dump() for key in keys],
        "filter_sql": filter_sql,
    }
    waking = await _wake_first(request, db, files, target, run_decided=False)
    if waking is not None:
        return waking
    try:
        return await _service(request).table_page(db, files, target, query)
    except _KERNEL_ERRORS as exc:
        return await _ask_refused(request, db, files, target, exc)


@router.get(
    "/{drive_id}/{item_id}/envs",
    response_model=EnvListing,
    dependencies=_DRIVE,
    responses=_ANSWERED,
)
async def notebook_envs(
    request: Request, drive_id: PathId, item_id: PathId, files: FilesCtx, db: Db
) -> Any:
    """The notebook's environment and the others it may use, asked of the box
    holding it. A reader who may run the notebook wakes its sleeping chat
    first and is answered ``notebook.waking``."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    waking = await _wake_first(request, db, files, target, run_decided=False)
    if waking is not None:
        return waking
    try:
        return await _service(request).envs(db, files, target)
    except _KERNEL_ERRORS as exc:
        return await _ask_refused(request, db, files, target, exc)


@router.get(
    # An environment's id names its spec root (``default:.alkera/envs/default``),
    # slashes and all: the rest of the path up to ``/packages`` is the id.
    "/{drive_id}/{item_id}/envs/{env_id:path}/packages",
    response_model=EnvPackages,
    dependencies=_DRIVE,
    responses=_ANSWERED,
)
async def env_packages(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    env_id: Annotated[str, Path(min_length=1, max_length=128, pattern=ID_PATTERN)],
) -> Any:
    """The packages installed in one of the notebook's environments."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    waking = await _wake_first(request, db, files, target, run_decided=False)
    if waking is not None:
        return waking
    try:
        return await _service(request).env_packages(db, files, target, env_id)
    except _KERNEL_ERRORS as exc:
        return await _ask_refused(request, db, files, target, exc)


@router.get(
    "/{drive_id}/{item_id}/widget-assets/resolve",
    response_model=WidgetAssetResolved,
    dependencies=_DRIVE,
)
async def resolve_widget_asset(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    module: Annotated[str, Query(min_length=1, max_length=214)],
    version: Annotated[str, Query(min_length=1, max_length=128)],
) -> Any:
    """The hash of a widget module's code: a platform bundle, or a module
    this notebook's kernel offered; 404 for anything else."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    found = _service(request).resolve_module(target.item_id, module, version)
    if found is None:
        raise NotFound()
    return WidgetAssetResolved(sha256=found)


@router.get(
    "/{drive_id}/{item_id}/widget-assets/{sha256}",
    dependencies=_DRIVE,
    responses={200: {"content": {"text/javascript": {}}}, 302: {"description": "Redirect"}},
)
async def widget_asset(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    sha256: Annotated[str, strict_path_id(SHA256_PATTERN)],
) -> Response:
    """Widget JavaScript by hash: a platform bundle, or an environment asset
    this notebook's kernel offered (never any other hash)."""
    target = await admit(request, db, files, item_id, FilesAction.READ)
    service = _service(request)
    bundle = service.bundles.get(sha256)
    if bundle is not None:
        return Response(
            content=bundle,
            media_type="text/javascript",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )
    if not service.feed.offered(target.item_id, sha256):
        raise NotFound()
    node = await service.blob_node(files, target, sha256, only_ext="js")
    if node is None:
        raise NotFound()
    return await _serve(request, db, files, node.id)


async def _serve(request: Request, db: AsyncSession, files: FilesCtx, node_id: Any) -> Response:
    """A redirect to a stored file's bytes, decided for EXPORT on that file."""
    allowed = await authorized(
        request, db, files, NodeId(uuid.UUID(str(node_id))), FilesAction.EXPORT
    )
    fresh = await fresh_bytes(
        request, db, files, allowed, deadline=files.settings.files_promote_wait_seconds
    )
    return RedirectResponse(
        await mint_download_url(request, files, fresh.allowed),
        status_code=302,
        headers=fresh.headers(),
    )


@router.post(
    "/{drive_id}/{item_id}/events",
    response_model=KernelEventsAccepted,
    dependencies=_DRIVE,
    responses={422: {"model": ErrorEnvelope}},
)
async def kernel_events(
    request: Request,
    drive_id: PathId,
    item_id: PathId,
    files: FilesCtx,
    db: Db,
    body: KernelEventsBatch,
) -> Any:
    """Box -> backend: the kernel's events. Taken only from the machine the
    kernel is bound to, in the kernel's own org; a kernel is bound on its
    first batch to the machine holding the notebook's folder."""
    ctx = files.ctx
    sender = ctx.acting_principal.id if ctx.is_machine else files.agent_machine_id
    if sender is None:
        raise NotFound()
    out_of_range = seq_out_of_range(body.events)
    if out_of_range is not None:
        # The kernel's sequence is the version its batches are announced
        # under: one past it can never be taken, and the box must hear so.
        return _error(
            422,
            "notebook.seq_out_of_range",
            f"an event's seq ({out_of_range}) is outside 0..{MAX_EVENT_SEQ}",
        )
    target = await admit(request, db, files, item_id, FilesAction.READ)
    holder = await folder_holder(db, ctx, target.item_id)
    try:
        accepted = await accept_events(
            db,
            org_id=target.org_id,
            drive_id=target.drive_id,
            item_id=target.item_id,
            sender_machine_id=sender,
            holder_machine_id=None if holder is None else holder.machine_id,
            kernel_id=body.kernel_id,
            state=body.state,
            events=body.events,
        )
    except KernelRefusedError:
        await db.rollback()
        raise NotFound() from None
    await follow_runs(
        db,
        target,
        machine_id=sender,
        kernel_id=accepted.kernel_id,
        events=accepted.events,
        at=datetime.now(UTC),
    )
    await db.commit()
    return KernelEventsAccepted(
        kernel_id=accepted.kernel_id, accepted=accepted.accepted, seq=accepted.seq
    )


__all__ = ["PREFIX", "router"]
