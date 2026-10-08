"""Compute routes: the catalog and a user's session allocations.

- ``GET  /api/v1/compute/catalog`` — the machine types, priced for the caller.
- ``GET  /api/v1/compute/allocations`` — the caller's allocations.
- ``POST /api/v1/compute/allocations`` — admit + provision a session machine.
- ``GET|DELETE /api/v1/compute/allocations/{id}``, ``POST .../renew``.

Every route decides through the ``compute.machine`` policy: a foreign
allocation is a 404 indistinguishable from a missing one, and every decision
is on record. A refused start answers ``402`` (insufficient credit) or ``429``
(the grant's ceiling, or no grant) with ``{code, message}`` — after the
refusal itself was put on the event outbox and the org audit trail. ``409`` is
a type out of stock; ``502`` a provider failure. No route returns an SSH
private key.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.compute.provider import ComputeProvider, ComputeProviderError, provider_for
from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.observability.envelope import ErrorEnvelope
from alkera_core.observability.redaction import scrub_text
from alkera_core.schemas.compute import (
    ComputeAllocationCreateRequest,
    ComputeAllocationInfo,
    ComputeRenewRequest,
    MachineTypeInfo,
)
from fastapi import APIRouter, HTTPException, Request, status

from backend.api.deps.compute import refused
from backend.api.params import PathId
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services.compute import grants
from backend.services.compute import service as compute_service

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["compute"])

_REFUSAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    402: {"model": ErrorEnvelope, "description": "Insufficient credit for the first minute"},
    429: {"model": ErrorEnvelope, "description": "The compute grant's ceiling is in use"},
}


def get_provider(machine_type: ComputeMachineType) -> ComputeProvider:
    """The provider that owns ``machine_type``, from the registry — module-level
    so tests substitute a fake. Every route resolves from the row it is acting
    on, so an allocation is created, polled and terminated through the one
    provider that knows its machine."""
    return provider_for(machine_type, settings)


#: What a caller is told when the compute provider fails a call. The provider's
#: own text can carry a runtime's stderr, instance ids and hosts, so it stays in
#: the server log and the answer is this fixed sentence under a client-safe code.
PROVIDER_FAILED_MESSAGE = "The compute provider could not complete the request. Try again shortly."


def _provider_failed(
    exc: ComputeProviderError, *, operation: str, machine_type: ComputeMachineType, **ids: str
) -> HTTPException:
    log.warning(
        "compute.provider_failed",
        operation=operation,
        machine_type_id=str(machine_type.id),
        provider=machine_type.provider,
        provider_status=exc.status_code,
        error=scrub_text(str(exc)),
        **ids,
    )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "provider_error", "message": PROVIDER_FAILED_MESSAGE},
    )


def _not_found(what: str = "Allocation") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _parse_uuid(raw: str, what: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise _not_found(what) from exc


async def _decide(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    *,
    action: Action,
    resource_id: str,
    is_owner: bool,
    lifecycle: str,
) -> None:
    roles = await role_resolver(request, db, ctx).for_team(ctx.org_id)
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.COMPUTE_MACHINE, id=resource_id, org_id=ctx.org_id),
        {
            "in_org": roles.in_org,
            "roles": roles.roles,
            "email_verified": user.email_verified_at is not None,
            "is_owner": is_owner,
            "lifecycle": lifecycle,
            # Every write here decides whether the allocation exists — starting
            # one, releasing it, renewing it. For a session allocation that is
            # the renter's own business; for the org's workspace box it takes
            # the same authority as standing one up, so a demoted operator
            # cannot take the org's compute away.
            "controls": action is Action.WRITE,
        },
    )


async def _owned(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    allocation_id: str,
    *,
    action: Action,
) -> ComputeAllocation:
    """The allocation, once the policy allowed ``action`` on it. A row in another
    org, another user's row and no row at all are one and the same 404."""
    alloc = await compute_service.get_allocation(
        db, allocation_id=_parse_uuid(allocation_id, "Allocation"), org_team_id=ctx.org_id
    )
    await _decide(
        request,
        db,
        ctx,
        user,
        action=action,
        resource_id=allocation_id,
        is_owner=alloc is not None and alloc.user_id == user.id,
        lifecycle=alloc.lifecycle if alloc is not None else "session",
    )
    if alloc is None:
        raise _not_found()
    return alloc


@router.get("/compute/catalog", response_model=list[MachineTypeInfo])
async def compute_catalog(
    request: Request, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
) -> list[MachineTypeInfo]:
    """The catalog, each type carrying the rate the CALLER would be billed under
    their grant (``None`` when nothing admits it)."""
    await _decide(
        request,
        db,
        ctx,
        user,
        action=Action.READ,
        resource_id="catalog",
        is_owner=True,
        lifecycle="",
    )
    out: list[MachineTypeInfo] = []
    for mt in await compute_service.list_machine_types(db):
        grant = await grants.resolve_compute_grant(
            db, ctx=ctx, org_team_id=ctx.org_id, machine_type=mt
        )
        out.append(
            compute_service.machine_type_info(
                mt, rate_per_minute_nanos=grant.rate_per_minute_nanos if grant else None
            )
        )
    return out


@router.get("/compute/allocations", response_model=list[ComputeAllocationInfo])
async def list_allocations(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    active: bool = False,
) -> list[ComputeAllocationInfo]:
    await _decide(
        request, db, ctx, user, action=Action.READ, resource_id="mine", is_owner=True, lifecycle=""
    )
    allocs = await compute_service.list_allocations(
        db, user_id=user.id, org_team_id=ctx.org_id, active_only=active
    )
    out: list[ComputeAllocationInfo] = []
    for alloc in allocs:
        mt = await compute_service.get_machine_type(db, alloc.machine_type_id)
        if mt is not None:
            out.append(compute_service.allocation_info(alloc, mt))
    return out


@router.post(
    "/compute/allocations",
    response_model=ComputeAllocationInfo,
    status_code=status.HTTP_201_CREATED,
    responses=_REFUSAL_RESPONSES,
)
async def create_allocation(
    request: Request,
    body: ComputeAllocationCreateRequest,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> ComputeAllocationInfo:
    mt = await compute_service.get_machine_type(
        db, _parse_uuid(body.machine_type_id, "Machine type")
    )
    if mt is None or not mt.active:
        raise _not_found("Machine type")
    await _decide(
        request,
        db,
        ctx,
        user,
        action=Action.WRITE,
        resource_id=str(mt.id),
        is_owner=True,
        lifecycle="session",
    )
    try:
        alloc = await compute_service.create_allocation(
            db,
            ctx=ctx,
            user=user,
            machine_type=mt,
            project_path=body.project_path,
            session_id=body.session_id,
            provider=get_provider(mt),
            max_lease_minutes=body.max_minutes,
        )
    except grants.ComputeRefusedError as exc:
        raise refused(exc) from exc
    except compute_service.MachineTypeUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ComputeProviderError as exc:
        raise _provider_failed(exc, operation="create", machine_type=mt) from exc
    return compute_service.allocation_info(alloc, mt)


@router.get("/compute/allocations/{allocation_id}", response_model=ComputeAllocationInfo)
async def get_allocation(
    request: Request,
    allocation_id: PathId,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> ComputeAllocationInfo:
    alloc = await _owned(request, db, ctx, user, allocation_id, action=Action.READ)
    mt = await compute_service.get_machine_type(db, alloc.machine_type_id)
    if mt is None:  # FK guarantees this can't happen; defensive for mypy/audits
        raise _not_found("Machine type")
    alloc = await compute_service.refresh_allocation(db, alloc, provider=get_provider(mt))
    return compute_service.allocation_info(alloc, mt)


@router.delete("/compute/allocations/{allocation_id}", response_model=ComputeAllocationInfo)
async def terminate_allocation(
    request: Request,
    allocation_id: PathId,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> ComputeAllocationInfo:
    alloc = await _owned(request, db, ctx, user, allocation_id, action=Action.WRITE)
    mt = await compute_service.get_machine_type(db, alloc.machine_type_id)
    if mt is None:
        raise _not_found("Machine type")
    try:
        alloc = await compute_service.terminate_allocation(
            db, alloc, provider=get_provider(mt), actor=ctx.audit_dict()
        )
    except ComputeProviderError as exc:
        raise _provider_failed(
            exc, operation="terminate", machine_type=mt, allocation_id=str(alloc.id)
        ) from exc
    return compute_service.allocation_info(alloc, mt)


@router.post("/compute/allocations/{allocation_id}/renew", response_model=ComputeAllocationInfo)
async def renew_allocation(
    request: Request,
    allocation_id: PathId,
    body: ComputeRenewRequest,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> ComputeAllocationInfo:
    """Set (renew/extend or clear) a session allocation's self-imposed lease
    ceiling; ``max_minutes=None`` clears the cap (unlimited)."""
    alloc = await _owned(request, db, ctx, user, allocation_id, action=Action.WRITE)
    alloc = await compute_service.renew_allocation(db, alloc, max_lease_minutes=body.max_minutes)
    mt = await compute_service.get_machine_type(db, alloc.machine_type_id)
    if mt is None:
        raise _not_found("Machine type")
    return compute_service.allocation_info(alloc, mt)
