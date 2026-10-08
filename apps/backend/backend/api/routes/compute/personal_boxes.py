"""A person's own boxes: list them, and take one's standing away.

A box registered through the device flow holds a machine credential that never
expires. Its person ends it here, on any of their own sign-ins; revoking is
immediate at every door (the box's next request is a 401 and placement stops
choosing it). Logout-all, a password reset and a ban end all of them at once.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.models import MachineCredential
from alkera_core.models.compute import PERSONAL_TENANCY
from alkera_core.schemas.compute import PersonalBoxList, PersonalBoxRead
from fastapi import APIRouter, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import CurrentOrg, CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services import credentials as credential_service

router = APIRouter(prefix="/api/v1/me/boxes", tags=["machines"])


def _read(row: MachineCredential) -> PersonalBoxRead:
    return PersonalBoxRead(
        id=str(row.id),
        label=row.label,
        org_id=str(row.org_team_id),
        machine_id=str(row.machine_id) if row.machine_id is not None else None,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


async def _decide(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    action: Action,
    *,
    box_id: str,
    owner: bool,
) -> None:
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.PERSONAL_BOX, id=box_id),
        {"is_owner": owner},
    )


@router.get("", response_model=PersonalBoxList)
async def list_my_boxes(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser, org_id: CurrentOrg
) -> PersonalBoxList:
    """The boxes on the caller's own hardware in the org they are signed in
    to, revoked ones included."""
    await _decide(request, db, ctx, Action.READ, box_id=str(user.id), owner=True)
    rows = await credential_service.list_personal(db, user.id, org_id=org_id)
    await db.commit()
    return PersonalBoxList(items=[_read(row) for row in rows])


@router.delete("/{box_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_box(
    request: Request,
    box_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    org_id: CurrentOrg,
) -> Response:
    """Take one of the caller's boxes' standing away. Idempotent; a box that
    is not a personal box of the caller's in the org they are signed in to is
    a 404 (their box in another org is that org's)."""
    row = await credential_service.get_machine_credential(db, box_id)
    owner = (
        row is not None
        and row.tenancy == PERSONAL_TENANCY
        and row.created_by == user.id
        and row.org_team_id == org_id
    )
    await _decide(request, db, ctx, Action.DELETE, box_id=str(box_id), owner=owner)
    await credential_service.revoke_machine_credential(db, box_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
