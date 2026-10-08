"""Admin: an org's compute grants.

Granting compute used to be a demo script, so a freshly onboarded org's box
answered `429 no_compute_grant` until somebody with a shell ran it. These three
routes are that act, in the product: read what an org holds, write (or update)
a grant, revoke one.

Reading sits on the router's ALKERA_SUPPORT floor; writing and revoking are
ALKERA_ADMIN, because a grant lets an org spend Alkera's provider money. Both
floors are decided by the ``compute.grant`` policy through ``enforce``, so the
decision is on record whichever way it goes — the dependency alone would refuse
without leaving a row.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.models._enums import PlatformRole
from alkera_core.models.compute import ComputeGrant, ComputeMachineType
from alkera_core.schemas.compute_admin import (
    ANY_MACHINE_TYPE,
    ANY_MACHINE_TYPE_LABEL,
    ComputeGrantRead,
    ComputeGrantUpsertRequest,
)
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services.compute import grants
from backend.services.org import teams as team_service

router = APIRouter(prefix="/orgs", route_class=AuditedRoute)

#: The grant resource is platform-scoped on purpose: the caller is staff looking
#: at somebody else's tenant, so binding the resource to the target org would
#: trip the engine's cross-org guard before the policy ever ran. The org travels
#: as a fact instead, and the policy decides on it.
_RESOURCE_TYPE = ResourceType.COMPUTE_GRANT


async def _decide(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    org_id: UUID,
    action: Action,
    resource_id: str,
) -> None:
    org = await team_service.get_by_id(db, org_id)
    exists = org is not None and org.is_root
    role = caller.platform_role
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(_RESOURCE_TYPE, id=resource_id),
        {
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "org_exists": exists,
            "target_org_id": str(org_id),
        },
    )


async def _read(db: AsyncSession, grant: ComputeGrant) -> ComputeGrantRead:
    team = await team_service.get_by_id(db, grant.org_team_id)
    machine_type = (
        await db.get(ComputeMachineType, grant.machine_type_id)
        if grant.machine_type_id is not None
        else None
    )
    # The wildcard is decided by the GRANT, never by whether the type row loaded:
    # a typed grant whose machine type has since been deleted must not read back
    # as "admits everything", which is the opposite of what it grants.
    if grant.machine_type_id is None:
        code, label = ANY_MACHINE_TYPE, ANY_MACHINE_TYPE_LABEL
    else:
        code = machine_type.provider_type_id if machine_type is not None else ""
        label = machine_type.display_name if machine_type is not None else ""
    return ComputeGrantRead(
        id=str(grant.id),
        org_team_id=str(grant.org_team_id),
        team_name=team.name if team is not None else "",
        machine_type_id=str(grant.machine_type_id) if grant.machine_type_id is not None else None,
        machine_type=code,
        machine_type_display_name=label,
        ceiling=grant.ceiling,
        per_user_max=grant.per_user_max,
        rate_per_minute_nanos=grant.rate_per_minute_nanos,
        expires_at=grant.expires_at,
        note=grant.note,
        created_at=grant.created_at,
    )


@router.get("/{org_id}/compute", response_model=list[ComputeGrantRead])
async def list_org_compute_grants(
    request: Request, org_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> list[ComputeGrantRead]:
    """Every live grant anywhere in the org's tree, newest first."""
    await _decide(
        request, db, ctx, caller, org_id=org_id, action=Action.READ, resource_id=str(org_id)
    )
    rows = await grants.list_grants(db, org_team_id=org_id)
    return [await _read(db, row) for row in rows]


@router.put(
    "/{org_id}/compute",
    response_model=ComputeGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_org_compute_grant(
    request: Request,
    response: Response,
    org_id: UUID,
    payload: ComputeGrantUpsertRequest,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> ComputeGrantRead:
    """Grant the org compute, or bring its live grant up to date.

    Answers 201 for a grant that did not exist and 200 when an existing one was
    updated — the same idempotence the provisioning script relies on.
    """
    await _decide(
        request, db, ctx, caller, org_id=org_id, action=Action.ADMIN, resource_id=str(org_id)
    )
    target = payload.org_team_id or org_id
    if target != org_id and target not in set(await team_service.descendant_ids(db, org_id)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The grant target must be the org or a team inside it",
        )
    machine_type_id = payload.machine_type_id
    if machine_type_id is not None:
        found = await db.execute(
            select(ComputeMachineType.id).where(ComputeMachineType.id == machine_type_id)
        )
        if found.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Machine type not found"
            )
    try:
        grant, created = await grants.upsert_grant(
            db,
            org_team_id=target,
            machine_type_id=machine_type_id,
            ceiling=payload.ceiling,
            rate_per_minute_nanos=payload.rate_per_minute_nanos,
            expires_at=payload.expires_at,
            note=payload.note,
            per_user_max=payload.per_user_max,
            created_by=caller.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return await _read(db, grant)


@router.delete("/{org_id}/compute/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_org_compute_grant(
    request: Request,
    org_id: UUID,
    grant_id: UUID,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> None:
    """Revoke one grant. A grant id from another org reads as not-found."""
    await _decide(
        request, db, ctx, caller, org_id=org_id, action=Action.ADMIN, resource_id=str(grant_id)
    )
    if not await grants.revoke_grant(db, grant_id=grant_id, org_team_id=org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
