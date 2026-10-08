"""``/api/v1/workspaces/{id}/machine``: where a workspace runs, and moving it.

``GET`` is the workspace header's machine: its card, its pin, the move under
way, whether the caller may move it, and where to. Reading it takes READ on
``workspace.access``.

``POST`` asks for a move (202 with the move). It decides through
``workspace_machine.move`` (Full access or Owner on the workspace, and use of
the target through the ``org_machine`` facts), takes ``If-Match`` with the
workspace's ``version``, and refuses with 409 ``move_target_not_shared`` when
someone with a chat in the workspace could not use the target, 409
``move_in_progress`` while another move runs, and the admission's 402 when a
stopped target cannot be started. A target machine of another org, deleted,
or missing is the ``org_machine`` policy's 404.

``POST .../moves/{move_id}/cancel`` cancels a move before its chats move.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.engine import authorize
from alkera_core.authz.policies import org_machine as org_machine_policy
from alkera_core.compute.workspace_move import active_move
from alkera_core.models import WorkspaceObject
from alkera_core.schemas.org_machines import (
    WorkspaceMachineMoveRead,
    WorkspaceMachineMoveRequest,
    WorkspaceMachineRead,
)
from fastapi import APIRouter, HTTPException, Request, status

from backend.api.deps.compute import refused
from backend.api.preconditions import IfMatch, require_current
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services import infra, objects, sharing, workspaces
from backend.services.compute import (
    ComputeRefusedError,
    OrgMachineRow,
    OrgMachineViewer,
    load_org_machine,
    org_machine_allowed,
    org_machine_attrs,
    org_machine_audiences,
    org_machine_resource,
    org_machine_viewer,
)
from backend.services.workspaces import (
    MoveRefusedError,
    MoveTarget,
    admit_move,
    cancel_move,
    default_machine_for,
    load_move,
    move_attrs,
    move_read,
    move_stalled,
    move_target_for,
    request_move,
    start_move_workflow,
    workspace_machine_read,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


async def _viewer(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> OrgMachineViewer:
    return await org_machine_viewer(
        db, ctx=ctx, user=user, resolver=role_resolver(request, db, ctx)
    )


async def _reader(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> sharing.Reader:
    return await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )


async def _load(db: DbSession, workspace_id: UUID) -> WorkspaceObject:
    workspace = await workspaces.load(db, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return workspace


def _resource(workspace: WorkspaceObject) -> Resource:
    return Resource(
        ResourceType.WORKSPACE_MACHINE, id=str(workspace.id), org_id=workspace.org_team_id
    )


async def _target_row(
    request: Request, db: DbSession, viewer: OrgMachineViewer, raw: str | None
) -> OrgMachineRow | None:
    """The org machine a move names, or ``None`` for the default placement.
    One that is missing, deleted or another org's is the ``org_machine``
    policy's not-found, on record."""
    if raw is None:
        return None
    row = await load_org_machine(db, org_id=viewer.org_id, machine_id=raw)
    if row is None:
        await enforce(
            request,
            db,
            viewer.ctx,
            Action.READ,
            org_machine_resource(None, viewer=viewer, id=raw),
            {"in_org": False, "purpose": org_machine_policy.USE_PURPOSE},
        )
        raise AssertionError("a machine not found is never allowed")  # pragma: no cover
    grants_of = await org_machine_audiences(db, org_id=viewer.org_id, machine_ids=[row.machine.id])
    seen = await org_machine_attrs(
        viewer, row.machine, grants_of[row.machine.id], purpose=org_machine_policy.READ_PURPOSE
    )
    if not org_machine_allowed(viewer, Action.READ, row.machine, seen):
        # A machine the caller may not even see is the same not-found as a
        # missing one, decided by its own policy and on record.
        await enforce(
            request,
            db,
            viewer.ctx,
            Action.READ,
            org_machine_resource(row.machine, viewer=viewer, id=""),
            {**seen, "purpose": org_machine_policy.USE_PURPOSE},
        )
    return row


def _refusal(exc: MoveRefusedError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail())


@router.get("/{workspace_id}/machine", response_model=WorkspaceMachineRead)
async def get_workspace_machine(
    request: Request,
    workspace_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> WorkspaceMachineRead:
    """Where the workspace runs, and where the caller may move it."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    await enforce(
        request,
        db,
        ctx,
        Action.READ,
        sharing.object_resource(workspace, type=ResourceType.WORKSPACE),
        attrs,
    )
    viewer = await _viewer(request, db, ctx, user)
    can_move = authorize(
        ctx,
        Action.WRITE,
        _resource(workspace),
        move_attrs(attrs, MoveTarget(row=None, usable=True)),
    ).allowed
    read = await workspace_machine_read(db, viewer=viewer, workspace=workspace, can_move=can_move)
    await db.commit()
    await _rearm_if_stalled(db, ctx=ctx, workspace=workspace)
    return read


async def _rearm_if_stalled(
    db: DbSession, *, ctx: CurrentPrincipal, workspace: WorkspaceObject
) -> None:
    """A move whose workflow has gone quiet is started again: the workflow id
    is the move's, so a run still in flight is attached to, never doubled."""
    move = await active_move(db, workspace_id=workspace.id, org_team_id=workspace.org_team_id)
    if move is None or not move_stalled(move, now=datetime.now(UTC)):
        return
    default = (
        await default_machine_for(db, ctx=ctx, workspace=workspace)
        if move.to_org_machine_id is None
        else None
    )
    await start_move_workflow(move, default)


@router.post(
    "/{workspace_id}/machine",
    response_model=WorkspaceMachineMoveRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def move_workspace(
    request: Request,
    workspace_id: UUID,
    payload: WorkspaceMachineMoveRequest,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    if_match: IfMatch = None,
) -> WorkspaceMachineMoveRead:
    """Move the workspace to another machine, or to the org's default
    placement (``to_org_machine_id`` null)."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    viewer = await _viewer(request, db, ctx, user)
    row = await _target_row(request, db, viewer, payload.to_org_machine_id)
    target = await move_target_for(db, viewer, row)
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    await enforce(request, db, ctx, Action.WRITE, _resource(workspace), move_attrs(attrs, target))
    # Admission queues on the grant, which every start takes before any chat
    # or workspace row: asked here, before the workspace is locked.
    try:
        await admit_move(db, ctx=ctx, target=target)
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    locked = await objects.object_service.lock(db, workspace.id)
    if locked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    require_current(if_match, str(locked.version), what="workspace")
    try:
        move = await request_move(
            db,
            ctx=ctx,
            user=user,
            workspace=locked,
            target=target,
            stop_running=payload.stop_running,
        )
    except MoveRefusedError as exc:
        raise _refusal(exc) from exc
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    default = await default_machine_for(db, ctx=ctx, workspace=locked) if row is None else None
    read = move_read(move)
    await db.commit()
    await start_move_workflow(move, default)
    if row is not None and row.machine.desired_power != "on":
        await infra.nudge_org_machine_reconcile()
    return read


@router.post(
    "/{workspace_id}/machine/moves/{move_id}/cancel",
    response_model=WorkspaceMachineMoveRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_workspace_move(
    request: Request,
    workspace_id: UUID,
    move_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> WorkspaceMachineMoveRead:
    """Cancel a move before its chats move; the pin goes back to where it was."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    # Canceling only puts back where the workspace already ran: the target's
    # audience is not the question, the workspace's is.
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        _resource(workspace),
        move_attrs(attrs, MoveTarget(row=None, usable=True)),
    )
    # The move before the workspace, the order the move's own steps take them in.
    move = await load_move(db, workspace=workspace, move_id=move_id)
    if move is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    locked = await objects.object_service.lock(db, workspace.id)
    if locked is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        await cancel_move(db, ctx=ctx, user=user, workspace=locked, move=move)
    except MoveRefusedError as exc:
        raise _refusal(exc) from exc
    read = move_read(move)
    await db.commit()
    return read
