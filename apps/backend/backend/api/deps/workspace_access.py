"""Deciding an action on a workspace, for every route that does."""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, ResourceType
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import User, WorkspaceObject
from fastapi import HTTPException, Request, status

from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services import sharing


async def workspace_reader(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> sharing.Reader:
    return await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )


async def decide_workspace(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    reader: sharing.Reader,
    workspace: WorkspaceObject,
    action: Action,
) -> dict[str, object]:
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    await enforce(
        request,
        db,
        ctx,
        action,
        sharing.object_resource(workspace, type=ResourceType.WORKSPACE),
        attrs,
    )
    return attrs


async def held_workspace(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: User | None,
    workspace_id: UUID,
    action: Action,
) -> tuple[WorkspaceObject, User]:
    """The workspace and its owner, once ``action`` is decided on it. Loaded
    from any org: another org's workspace is the engine's to refuse, on
    record."""
    workspace = await sharing.load_object(db, workspace_id, type=WORKSPACE_TYPE)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    reader = (
        sharing.machine_reader(ctx)
        if ctx.is_machine or user is None
        else await workspace_reader(request, db, ctx, user)
    )
    await decide_workspace(request, db, ctx, reader, workspace, action)
    owner = await db.get(User, workspace.owner_user_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return workspace, owner
