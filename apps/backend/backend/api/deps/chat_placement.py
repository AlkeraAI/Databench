"""Where a new chat runs, as the create route answers it.

A chat in a workspace that owns a folder goes where its siblings are, and only
on a box that said it can run a workspace (:func:`placement.place_new_chat`).
When a box is up but none can, the chat would run without the workspace's
files, so the create is refused with a reason a client can show instead.
"""

from __future__ import annotations

from typing import Any

from alkera_core.models import WorkspaceObject
from alkera_core.schemas.objects.specs import CloudPermissionMode
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import workspaces
from backend.services.compute import (
    CapabilityMissingError,
    MachineBinding,
    place_new_chat,
)


async def placed(
    db: AsyncSession,
    *,
    ctx: Any,
    user: Any,
    mode: CloudPermissionMode | None,
    workspace: WorkspaceObject | None,
) -> MachineBinding | None:
    """The machine a new chat in ``workspace`` (or the owner's default) is
    bound to; a 409 ``workspace_box_unsupported`` when no box can run it."""
    target = workspace or await workspaces.default_target(db, owner=user, org_id=ctx.org_id)
    try:
        return await place_new_chat(db, ctx=ctx, user=user, mode=mode, workspace=target)
    except CapabilityMissingError as missing:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": missing.code, "message": str(missing)},
        ) from missing


__all__ = ["placed"]
