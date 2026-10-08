"""Admin: the machines Alkera sells.

- ``GET   /admin/v1/compute/offerings`` — every offering, retired ones
  included, with the provider's live price beside the customer rate it
  yields. Staff floor.
- ``POST  /admin/v1/compute/offerings`` — add one. Platform admin.
- ``PATCH /admin/v1/compute/offerings/{id}`` — change one, retire it
  (``retired: true``) or bring it back. Platform admin.

Every decision goes through ``enforce`` on the ``compute.offering`` policy, so
it is on record whichever way it goes. A refused write answers
``{code, message}``: 422 for a row that cannot be sold as sent, 409 for one
that conflicts with machines already bought from it.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Resource, ResourceType
from alkera_core.authz.policies import compute_offering as policy
from alkera_core.models import PlatformRole
from alkera_core.schemas.org_machines import OfferingAdminRead, OfferingCreate, OfferingUpdate
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services import compute

router = APIRouter(prefix="/compute/offerings", route_class=AuditedRoute)


async def decide_platform(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    operation: str,
    resource_id: str,
    org_exists: bool = True,
) -> None:
    """Decide a platform catalog or grant operation and put it on record."""
    role = caller.platform_role
    await enforce(
        request,
        db,
        ctx,
        policy.OPERATIONS[operation],
        Resource(ResourceType.COMPUTE_OFFERING, id=resource_id),
        {
            "operation": operation,
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "org_exists": org_exists,
        },
    )


def refusal(exc: compute.OfferingError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("", response_model=list[OfferingAdminRead])
async def list_offerings(
    request: Request, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> list[OfferingAdminRead]:
    await decide_platform(request, db, ctx, caller, operation=policy.LIST, resource_id="catalog")
    return await compute.admin_offerings(db)


@router.post("", response_model=OfferingAdminRead, status_code=status.HTTP_201_CREATED)
async def create_offering(
    request: Request,
    payload: OfferingCreate,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> OfferingAdminRead:
    await decide_platform(request, db, ctx, caller, operation=policy.CREATE, resource_id="catalog")
    try:
        offering = await compute.create_offering(db, payload, created_by=caller.id)
    except compute.OfferingError as exc:
        raise refusal(exc) from exc
    return await compute.admin_offering(db, offering.id)


@router.patch("/{offering_id}", response_model=OfferingAdminRead)
async def update_offering(
    request: Request,
    offering_id: UUID,
    payload: OfferingUpdate,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> OfferingAdminRead:
    await decide_platform(
        request, db, ctx, caller, operation=policy.UPDATE, resource_id=str(offering_id)
    )
    try:
        await compute.update_offering(db, offering_id, payload)
    except compute.OfferingError as exc:
        raise refusal(exc) from exc
    return await compute.admin_offering(db, offering_id)


__all__ = ["decide_platform", "refusal", "router"]
