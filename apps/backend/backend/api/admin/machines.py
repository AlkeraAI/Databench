"""Admin: the chat boxes the platform runs, and which org each is dedicated to.

A platform admin provisions an EC2 instance by hand (outside terraform), mints
a credential for it here, boots the box with the credential, and watches it
register. A box minted as ``pool`` serves the chats of every org that has no
compute of its own; one minted as ``dedicated`` serves nothing until it is
assigned to exactly one org, and then serves that org and no other.

- ``GET    /admin/v1/machines`` — every credential ever minted, with the live
  registration behind it (heartbeat, chats served, daemon version, the org it
  is dedicated to), and every machine still standing that no credential names
  (an org's own box, registered under its grant). Staff floor.
- ``POST   /admin/v1/machines`` — mint a credential. The raw secret is in this
  one answer and never again. Platform admin.
- ``DELETE /admin/v1/machines/{credential_id}`` — revoke: the box's next
  heartbeat is refused and the meter reaps its row; an org it was held for
  returns to the shared machines. Platform admin.
- ``GET    /admin/v1/orgs/{org_id}/compute/dedicated`` — the box behind the
  org's granted pool machine, if any. Staff floor.
- ``PUT    /admin/v1/orgs/{org_id}/compute/dedicated`` — hold a dedicated box
  as the org's granted pool machine (``machine_id``), with or without
  ``fallback_to_pool``, or let it go (``machine_id: null``). Platform admin.
- ``GET    /admin/v1/orgs/{org_id}/machines`` — the machines the org holds,
  bought or given. Staff floor.
- ``POST   /admin/v1/orgs/{org_id}/machines`` — give the org a machine of an
  offering, free until a date. Platform admin.

Every decision goes through ``enforce`` (the ``platform.machine`` policy for
the boxes, ``compute.offering`` for an org's machines) so it is on record
whichever way it goes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import get_args
from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.policies import compute_offering as offering_policy
from alkera_core.compute import availability as availability_core
from alkera_core.compute.availability import Availability, SizeQuery, unknown
from alkera_core.compute.machines import machine_state
from alkera_core.compute.provider import (
    ComputeProviderError,
    ProviderPod,
    provider_traits,
    registered_kinds,
)
from alkera_core.compute.reconcile import pod_name_for
from alkera_core.compute.unservable import fault_read, isolation_read
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.logging import get_logger
from alkera_core.models import (
    MachineCredential,
    OrgComputeAssignment,
    PlatformRole,
    Team,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute import (
    FAILED,
    RELEASED,
    ComputeAllocation,
    ComputeAllocationEvent,
    ComputeMachineType,
)
from alkera_core.models.org_machines import (
    OrgComputeSettings,
    OrgMachine,
)
from alkera_core.observability.redaction import scrub_text
from alkera_core.schemas.compute_machines import (
    DrainRequest,
    EventActor,
    MachineChat,
    MachineCost,
    MachineDetail,
    MachineDrain,
    MachineEvent,
    MachineReleased,
    MachineResources,
    MachineRow,
    MachineTypeAvailability,
    MachineTypeList,
    MachineTypeQuota,
    MachineTypeRow,
    MachineTypeStorage,
    OrgRef,
    ProviderNote,
    ProviderStatus,
    ProvisionProvider,
    ProvisionRequest,
    TerminateRequest,
    UnmanagedMachine,
    UnmanagedMachineList,
)
from alkera_core.schemas.machine_admin import (
    MachineMinted,
    MachineMintRequest,
    OrgComputeAssignmentRead,
    OrgComputeAssignmentUpdate,
    PlatformMachineRead,
)
from alkera_core.schemas.org_machines import (
    AcquisitionLiteral,
    OrgMachineGrant,
    OrgMachineRead,
)
from alkera_core.status import fleet_status
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.api.admin.offerings import decide_platform as decide_offering
from backend.api.admin.offerings import refusal as offering_refusal
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services.audit import record_org_audit
from backend.services.chats import chat_service
from backend.services.compute import (
    OfferingError,
    assign_dedicated,
    box_revoked,
    can_offer,
    grant_machine,
    granted_pool_machine,
    legacy_assignment_org,
    placement,
    platform_org_machine_reads,
    provisioning,
    unassign_dedicated,
)
from backend.services.compute import machines as machine_service
from backend.services.compute import service as compute_service
from backend.services.compute.machines import ChatLoad
from backend.services.credentials import machine_credentials as machine_credential_service
from backend.services.org import teams as team_service

log = get_logger(__name__)

router = APIRouter(route_class=AuditedRoute)

#: The code a mint the credential service refuses answers with. The refusal's
#: own text stays in the server log: a ``ValueError`` raised anywhere under the
#: mint would otherwise reach the caller verbatim.
MACHINE_MINT_REFUSED = "machine_mint_refused"

_RESOURCE_TYPE = ResourceType.PLATFORM_MACHINE

# The states past which a machine is history, not fleet: a lost box stays on
# the page, since an admin has to see a box that stopped answering.
_GONE_STATES: tuple[str, ...] = (RELEASED, FAILED)

#: The providers the console provisions through, in the order it offers them.
_PROVISION_KINDS: tuple[ProvisionProvider, ...] = get_args(ProvisionProvider)


def _provision_kinds() -> tuple[ProvisionProvider, ...]:
    """The providers the console provisions through that this deployment
    registers, in the order the console offers them."""
    registered = set(registered_kinds())
    return tuple(kind for kind in _PROVISION_KINDS if kind in registered)


#: How long one provider's machine listing may take before the page moves on.
_LIST_BUDGET_S = 5.0


async def _decide(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    action: Action,
    operation: str,
    resource_id: str,
    org_exists: bool = True,
) -> None:
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
            "operation": operation,
            "org_exists": org_exists,
        },
    )


async def _org_exists(db: AsyncSession, org_id: UUID) -> bool:
    org = await team_service.get_by_id(db, org_id)
    return org is not None and org.is_root


async def _served(
    db: AsyncSession, alloc: ComputeAllocation | None, served: dict[UUID, ChatLoad] | None
) -> ChatLoad:
    """The chats bound to ``alloc``, split by what the machine can do for
    them: from ``served`` when the caller counted a whole page at once, else
    counted for this one machine."""
    if alloc is None:
        return ChatLoad()
    if served is None:
        served = await machine_service.chat_load(db, [alloc.id])
    return served.get(alloc.id, ChatLoad())


async def _read(
    db: AsyncSession,
    credential: MachineCredential,
    *,
    now: datetime | None = None,
    served: dict[UUID, ChatLoad] | None = None,
) -> PlatformMachineRead:
    machine_type = await db.get(ComputeMachineType, credential.machine_type_id)
    alloc = (
        await db.get(ComputeAllocation, credential.machine_id)
        if credential.machine_id is not None
        else None
    )
    assigned = await _dedicated_org(db, alloc) if alloc is not None else None
    return PlatformMachineRead(
        credential_id=str(credential.id),
        label=credential.label,
        provider=machine_type.provider if machine_type is not None else "",
        instance_type=machine_type.provider_type_id if machine_type is not None else "",
        region=credential.region,
        tenancy=credential.tenancy,  # type: ignore[arg-type]
        created_at=credential.created_at,
        revoked_at=credential.revoked_at,
        last_used_at=credential.last_used_at,
        machine_id=str(alloc.id) if alloc is not None else None,
        machine_name=alloc.name if alloc is not None else "",
        status=machine_state(alloc, now=now),
        last_heartbeat_at=alloc.last_heartbeat_at if alloc is not None else None,
        chats_served=(await _served(db, alloc, served)).served,
        capacity=alloc.capacity if alloc is not None else 0,
        daemon_version=alloc.daemon_version if alloc is not None else "",
        true_cost_per_minute_nanos=(
            machine_type.provider_price_per_minute_nanos if machine_type is not None else 0
        ),
        assigned_org_id=str(assigned.id) if assigned is not None else None,
        assigned_org_name=assigned.name if assigned is not None else "",
    )


def _liveness(alloc: ComputeAllocation | None, now: datetime | None) -> str:
    return machine_state(alloc, now=now)


async def _org_machine_of(db: AsyncSession, alloc: ComputeAllocation) -> OrgMachine | None:
    """The live org machine ``alloc`` backs now, if any."""
    if alloc.org_machine_id is None:
        return None
    om = await db.get(OrgMachine, alloc.org_machine_id)
    if om is None or om.deleted_at is not None or om.current_allocation_id != alloc.id:
        return None
    return om


async def _dedicated_org(db: AsyncSession, alloc: ComputeAllocation) -> Team | None:
    """The org whose machine ``alloc`` is: the org of the org machine it backs,
    else the org a dedicated box was provisioned for."""
    om = await _org_machine_of(db, alloc)
    org_id = om.org_team_id if om is not None else None
    if org_id is None:
        org_id = await legacy_assignment_org(db, alloc)
    if org_id is None:
        return None
    return await team_service.get_by_id(db, org_id)


def _machine_fields(
    alloc: ComputeAllocation,
    machine_type: ComputeMachineType | None,
    org: Team | None,
    *,
    now: datetime | None,
    served: ChatLoad,
) -> dict[str, object]:
    resources = alloc.resources_json
    return {
        "id": str(alloc.id),
        "name": alloc.name,
        "provider": machine_type.provider if machine_type is not None else "",
        "provider_machine_id": alloc.provider_machine_id,
        "machine_type_code": machine_type.provider_type_id if machine_type is not None else "",
        "state": alloc.state,
        "liveness": _liveness(alloc, now),
        "tenancy": alloc.tenancy,
        "sandbox": alloc.sandbox,
        "dedicated_org": OrgRef(id=str(org.id), name=org.name) if org is not None else None,
        "capacity": alloc.capacity,
        "chats_served": served.served,
        "chats_stranded": served.stranded,
        "chats_asleep": served.asleep,
        "storage_gb": alloc.storage_gb,
        "heartbeat_at": alloc.last_heartbeat_at,
        "daemon_version": alloc.daemon_version,
        "created_at": alloc.created_at,
        "state_changed_at": alloc.state_changed_at,
        "drain": (
            MachineDrain(requested_at=alloc.drain_requested_at, reason=alloc.drain_reason)
            if alloc.drain_requested_at is not None
            else None
        ),
        "wake_requested_at": alloc.wake_requested_at,
        "cost": MachineCost(
            price_per_minute_nanos=alloc.true_cost_per_minute_nanos,
            minutes_billed=alloc.minutes_billed,
            true_cost_nanos=alloc.true_cost_nanos,
        ),
        "resources": MachineResources.model_validate(resources) if resources else None,
        "isolation": isolation_read(alloc),
        "fault": fault_read(alloc, staff=True),
        "origin": alloc.origin,
        "machine_id": str(alloc.id),
        "machine_name": alloc.name,
        "status": _liveness(alloc, now),
        "last_heartbeat_at": alloc.last_heartbeat_at,
        "assigned_org_id": str(org.id) if org is not None else None,
        "assigned_org_name": org.name if org is not None else "",
    }


async def _row(
    db: AsyncSession,
    credential: MachineCredential,
    *,
    now: datetime | None = None,
    served: dict[UUID, ChatLoad] | None = None,
) -> MachineRow:
    """One credential's row: the picker's fields, and the machine's when it has one."""
    legacy = await _read(db, credential, now=now, served=served)
    fields: dict[str, object] = {
        **legacy.model_dump(),
        "id": legacy.machine_id or legacy.credential_id,
        "name": legacy.label,
        "machine_type_code": legacy.instance_type,
        "liveness": legacy.status,
        "tenancy": credential.tenancy,
        "true_cost_per_minute_nanos": legacy.true_cost_per_minute_nanos,
    }
    if credential.machine_id is not None:
        alloc = await db.get(ComputeAllocation, credential.machine_id)
        if alloc is not None:
            machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
            org = await _dedicated_org(db, alloc)
            fields.update(
                _machine_fields(
                    alloc, machine_type, org, now=now, served=await _served(db, alloc, served)
                )
            )
    fields["credential_id"] = str(credential.id)
    fields["region"] = credential.region
    row = MachineRow.model_validate(fields)
    row.status_fact = fleet_status(
        allocation_state=row.state if credential.machine_id is not None else None,
        liveness=row.liveness,
        name=row.name,
        revoked=credential.revoked_at is not None,
        wake_pending=row.wake_requested_at is not None,
    )
    return row


async def _alloc_row(db: AsyncSession, alloc: ComputeAllocation) -> MachineRow:
    """A machine's row, through its live credential (the newest when none is)."""
    await db.refresh(alloc)
    credential = (
        await db.execute(
            select(MachineCredential)
            .where(MachineCredential.machine_id == alloc.id)
            .order_by(
                MachineCredential.revoked_at.is_(None).desc(),
                MachineCredential.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if credential is not None:
        return await _row(db, credential)
    machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
    org = await _dedicated_org(db, alloc)
    return MachineRow.model_validate(
        _machine_fields(alloc, machine_type, org, now=None, served=await _served(db, alloc, None))
    )


async def _chats_on(db: AsyncSession, alloc: ComputeAllocation) -> list[MachineChat]:
    chats = (
        (
            await db.execute(
                select(WorkspaceObject)
                .where(
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.deleted_at == 0,
                    WorkspaceObject.spec["machine_id"].astext == str(alloc.id),
                )
                .order_by(WorkspaceObject.updated_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    out: list[MachineChat] = []
    now = datetime.now(UTC)
    for chat in chats:
        org = await team_service.get_by_id(db, chat.org_team_id)
        owner = await db.get(User, chat.owner_user_id)
        out.append(
            MachineChat(
                id=str(chat.id),
                title=chat.title,
                org=OrgRef(id=str(chat.org_team_id), name=org.name if org is not None else ""),
                user={
                    "id": str(chat.owner_user_id),
                    "email": owner.email if owner is not None else None,
                },
                # What the chat says about THIS machine now, the way the chat
                # page says it — never the word its spec recorded at binding,
                # which reads ``ready`` on a box that has since been released.
                machine_status=placement.chat_machine_status(
                    chat_service.chat_spec_of(chat), alloc, now=now
                ),
                last_activity_at=chat.updated_at,
            )
        )
    return out


async def _events_of(db: AsyncSession, alloc: ComputeAllocation) -> list[MachineEvent]:
    rows = (
        (
            await db.execute(
                select(ComputeAllocationEvent)
                .where(ComputeAllocationEvent.allocation_id == alloc.id)
                .order_by(ComputeAllocationEvent.at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [
        MachineEvent(
            at=row.at,
            from_state=row.from_state,
            to_state=row.to_state,
            reason=row.reason,
            actor=EventActor.model_validate(row.actor or {}),
        )
        for row in rows
    ]


class FleetOrgMachine(BaseModel):
    """The org machine a fleet box backs now, and the org holding it."""

    id: str
    name: str
    org_id: str
    org_name: str


class FleetMachineRow(MachineRow):
    org_machine: FleetOrgMachine | None = None
    acquisition: AcquisitionLiteral | None = None


class FleetMachineList(BaseModel):
    items: list[FleetMachineRow]


async def _fleet_rows(db: AsyncSession, items: list[MachineRow]) -> list[FleetMachineRow]:
    """Each row with the org machine its box backs now, when it backs one."""
    ids: list[UUID] = []
    for item in items:
        if item.machine_id:
            try:
                ids.append(UUID(item.machine_id))
            except ValueError:
                continue
    held = (
        (
            await db.execute(
                select(OrgMachine).where(
                    OrgMachine.current_allocation_id.in_(ids), OrgMachine.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
        if ids
        else []
    )
    by_alloc = {str(om.current_allocation_id): om for om in held}
    orgs: dict[UUID, Team | None] = {}
    out: list[FleetMachineRow] = []
    for item in items:
        om = by_alloc.get(item.machine_id or "")
        org_machine: FleetOrgMachine | None = None
        if om is not None:
            if om.org_team_id not in orgs:
                orgs[om.org_team_id] = await team_service.get_by_id(db, om.org_team_id)
            org = orgs[om.org_team_id]
            org_machine = FleetOrgMachine(
                id=str(om.id),
                name=om.name,
                org_id=str(om.org_team_id),
                org_name=org.name if org is not None else "",
            )
        out.append(
            FleetMachineRow.model_validate(
                {
                    **item.model_dump(),
                    "org_machine": org_machine,
                    "acquisition": om.acquisition if om is not None else None,
                }
            )
        )
    return out


@router.get("/machines", response_model=FleetMachineList)
async def list_platform_machines(
    request: Request,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
    include_gone: bool = Query(default=False),
) -> FleetMachineList:
    """Every platform box: one row per credential minted (the dedicated-box
    picker's fields), with the machine behind it — provisioned or registered —
    its lifecycle, cost and load. A machine that is released or failed is
    history, listed only with ``include_gone``; a lost one stays, since an admin
    has to see a box that stopped answering."""
    await _decide(
        request, db, ctx, caller, action=Action.READ, operation="list", resource_id="machines"
    )
    credentials = await machine_credential_service.list_all(db)
    now = datetime.now(UTC)
    standing_filter = [] if include_gone else [ComputeAllocation.state.not_in(_GONE_STATES)]
    standing = (
        (
            await db.execute(
                select(ComputeAllocation)
                .where(*standing_filter)
                .order_by(ComputeAllocation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    machine_ids = {alloc.id for alloc in standing} | {
        row.machine_id for row in credentials if row.machine_id is not None
    }
    served = await machine_service.chat_load(db, machine_ids, now=now)
    items = [await _row(db, row, now=now, served=served) for row in credentials]
    if not include_gone:
        items = [item for item in items if item.state not in _GONE_STATES or not item.machine_id]
    # A box an org registered under its own grant carries no credential, so the
    # credential walk alone never reaches it; every machine still standing that
    # no credential names is listed on its own row.
    claimed = {row.machine_id for row in credentials if row.machine_id is not None}
    for alloc in standing:
        if alloc.id in claimed:
            continue
        machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
        org = await _dedicated_org(db, alloc)
        items.append(
            MachineRow.model_validate(
                _machine_fields(
                    alloc, machine_type, org, now=now, served=served.get(alloc.id, ChatLoad())
                )
            )
        )
    return FleetMachineList(items=await _fleet_rows(db, items))


@router.get("/machine-types", response_model=MachineTypeList)
async def list_machine_types(
    request: Request, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> MachineTypeList:
    """The sizes the console may provision, with our cost and whether this
    deployment can start one."""
    await _decide(
        request, db, ctx, caller, action=Action.READ, operation="types", resource_id="machines"
    )
    types = (
        (
            await db.execute(
                select(ComputeMachineType)
                .where(
                    ComputeMachineType.active.is_(True),
                    ComputeMachineType.provider.in_(_provision_kinds()),
                )
                .order_by(
                    ComputeMachineType.provider,
                    ComputeMachineType.provider_price_per_minute_nanos,
                )
            )
        )
        .scalars()
        .all()
    )
    answers: dict[str, dict[str, Availability]] = {}
    configured: dict[str, bool] = {
        kind: provisioning.make_node_provider(kind, settings).configured()
        for kind in _provision_kinds()
    }
    for kind in sorted({t.provider for t in types}):
        provider = provisioning.make_node_provider(kind, settings)
        sizes = [
            SizeQuery(code=t.provider_type_id, vcpu=t.vcpu) for t in types if t.provider == kind
        ]
        # ``availability`` is on the provider interface, so it is reached
        # directly (never probed off-Protocol): a provider this deployment
        # cannot act through is skipped by the ``configured`` gate below, not by
        # the method being absent.
        probe = provider.availability if configured[kind] else None
        answers[kind] = await availability_core.CACHE.lookup(kind, sizes, probe)
        if not configured[kind]:
            answers[kind] = {s.code: unknown(f"{kind} is not configured here") for s in sizes}
    items: list[MachineTypeRow] = []
    for t in types:
        live = answers[t.provider][t.provider_type_id]
        items.append(
            MachineTypeRow(
                id=str(t.id),
                provider=t.provider,
                code=t.provider_type_id,
                vcpu=t.vcpu,
                memory_gb=t.memory_gb,
                gpu=t.gpu_count,
                storage=MachineTypeStorage(
                    min_gb=10,
                    max_gb=provider_traits(t.provider).data_disk[0],
                    kind=provider_traits(t.provider).data_disk[1],
                ),
                price_per_minute_nanos=t.provider_price_per_minute_nanos,
                available=configured[t.provider]
                and t.available_for_new
                and live.status != "unavailable",
                can_offer=can_offer(t),
                availability=MachineTypeAvailability(
                    status=live.status, detail=live.detail, checked_at=live.checked_at
                ),
                quota=(
                    MachineTypeQuota(
                        vcpu_limit=live.quota.vcpu_limit,
                        vcpu_in_use=live.quota.vcpu_in_use,
                        vcpu_available=live.quota.vcpu_available,
                    )
                    if live.quota is not None
                    else None
                ),
            )
        )
    return MachineTypeList(
        items=items,
        providers=[
            ProviderStatus(
                kind=kind,
                configured=configured[kind],
                reason="" if configured[kind] else provider_traits(kind).unconfigured_reason,
            )
            for kind in _provision_kinds()
        ],
    )


async def _listed(kind: str) -> list[ProviderPod] | str:
    """Every machine ``kind``'s account holds, or why it could not be listed.
    Bounded: the fleet page must not hang on a slow provider."""
    provider = provisioning.make_node_provider(kind, settings)
    if not provider.configured():
        return []
    shown = provider_traits(kind).console_name or kind
    try:
        return await asyncio.wait_for(provider.list_pods(name_prefix=""), _LIST_BUDGET_S)
    except TimeoutError:
        return f"{shown} did not answer within {_LIST_BUDGET_S:g} s"
    except ComputeProviderError as exc:
        return provisioning.provider_reason(f"{shown} could not be listed", exc)


@router.get("/machines/unmanaged", response_model=UnmanagedMachineList)
async def list_unmanaged_machines(
    request: Request, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> UnmanagedMachineList:
    """The machines each configured provider holds that no allocation row owns
    (a box started by hand, or one a lost create left behind). Read-only: the
    plane never acts on a machine it has no row for, and neither does this page.
    A provider that cannot be listed is named in ``unavailable``, never read as
    holding nothing."""
    await _decide(
        request, db, ctx, caller, action=Action.READ, operation="list", resource_id="machines"
    )
    items: list[UnmanagedMachine] = []
    unavailable: list[ProviderNote] = []
    for kind in _provision_kinds():
        listed = await _listed(kind)
        if isinstance(listed, str):
            unavailable.append(ProviderNote(provider=kind, detail=listed))
            continue
        if not listed:
            continue
        ids = [pod.pod_id for pod in listed]
        owned = set(
            (
                await db.execute(
                    select(ComputeAllocation.provider_machine_id).where(
                        ComputeAllocation.provider_machine_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        # A create whose machine id never reached the row is still owned: the
        # reconcile adopts it by the name the plane gave it.
        in_flight = (
            (
                await db.execute(
                    select(ComputeAllocation.id).where(
                        ComputeAllocation.provider_machine_id == "",
                        ComputeAllocation.state.not_in(_GONE_STATES),
                    )
                )
            )
            .scalars()
            .all()
        )
        named = {pod_name_for(alloc_id) for alloc_id in in_flight}
        items.extend(
            UnmanagedMachine(
                provider=kind,
                provider_machine_id=pod.pod_id,
                name=pod.name,
                phase=pod.phase,
                raw_status=pod.raw_status,
                created_at=pod.created_at,
            )
            for pod in listed
            if pod.pod_id not in owned and pod.name not in named
        )
    return UnmanagedMachineList(items=items, unavailable=unavailable)


def _refusal(exc: provisioning.ProvisionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


async def _machine(
    db: AsyncSession, machine_id: UUID, *, org_boxes: bool = False, lock: bool = False
) -> ComputeAllocation:
    """The machine a route names. An org's own box is listed and readable,
    but the lifecycle actions are the platform's to take on its boxes only.

    ``lock`` reads the row fresh under ``FOR UPDATE``, for a route that moves
    its state: the reconcile or a claim may have moved it since the session
    last saw it, and an edge checked against a stale state would overwrite
    theirs (an undrain turning ``releasing`` back into ``ready``)."""
    if lock:
        alloc = (
            await lock_rows(
                db,
                LockRank.ALLOCATION,
                select(ComputeAllocation)
                .where(ComputeAllocation.id == machine_id)
                .execution_options(populate_existing=True),
            )
        ).scalar_one_or_none()
    else:
        alloc = await db.get(ComputeAllocation, machine_id)
    if alloc is None or (alloc.tenancy == "org" and not org_boxes):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")
    return alloc


@router.post("/machines/provision", response_model=MachineRow, status_code=status.HTTP_202_ACCEPTED)
async def provision_machine(
    request: Request,
    payload: ProvisionRequest,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> MachineRow:
    """Start a machine at the provider. Answers once the provider has taken
    the request; the machine is ``provisioning`` until its daemon claims it."""
    org_id: UUID | None = None
    if payload.org_id is not None:
        try:
            org_id = UUID(payload.org_id)
        except ValueError:
            org_id = None
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="provision",
        resource_id="machines",
        org_exists=payload.org_id is None or (org_id is not None and await _org_exists(db, org_id)),
    )
    try:
        alloc = await provisioning.provision(
            db,
            caller=caller,
            acting_org_id=ctx.org_id,
            provider_kind=payload.provider,
            machine_type_code=payload.machine_type_code,
            storage_gb=payload.storage_gb,
            tenancy=payload.tenancy,
            org_id=org_id,
            name=payload.name,
        )
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    return await _alloc_row(db, alloc)


@router.get("/machines/{machine_id}", response_model=MachineDetail)
async def get_machine(
    request: Request, machine_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> MachineDetail:
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.READ,
        operation="detail",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, org_boxes=True)
    row = await _alloc_row(db, alloc)
    return MachineDetail(
        **row.model_dump(),
        chats=await _chats_on(db, alloc),
        events=await _events_of(db, alloc),
    )


@router.post("/machines/{machine_id}/drain", response_model=MachineRow)
async def drain_machine(
    request: Request,
    machine_id: UUID,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
    payload: DrainRequest | None = None,
) -> MachineRow:
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="drain",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, lock=True)
    body = payload or DrainRequest()
    try:
        await provisioning.drain(
            db, alloc, caller=caller, reason=body.reason or "", auto_terminate=body.auto_terminate
        )
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    return await _alloc_row(db, alloc)


@router.post("/machines/{machine_id}/undrain", response_model=MachineRow)
async def undrain_machine(
    request: Request, machine_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> MachineRow:
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="undrain",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, lock=True)
    try:
        await provisioning.undrain(db, alloc, caller=caller)
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    return await _alloc_row(db, alloc)


@router.post("/machines/{machine_id}/sleep", response_model=MachineRow)
async def sleep_machine(
    request: Request, machine_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> MachineRow:
    """Sleep a ready machine: stopped at the provider (its disk kept) so it stops
    costing compute and is no longer metered, its chats handed on. ``409`` unless
    the machine is ``ready``."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="sleep",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, lock=True)
    try:
        await provisioning.sleep(db, alloc, caller=caller)
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    return await _alloc_row(db, alloc)


@router.post("/machines/{machine_id}/wake", response_model=MachineRow)
async def wake_machine(
    request: Request, machine_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> MachineRow:
    """Wake a sleeping machine back to ``ready``; metering resumes from now, so
    the stopped window is never billed. ``409`` unless the machine is ``asleep``."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="wake",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, lock=True)
    try:
        await provisioning.wake(db, alloc, caller=caller)
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    return await _alloc_row(db, alloc)


@router.post(
    "/machines/{machine_id}/terminate",
    response_model=MachineReleased,
    status_code=status.HTTP_202_ACCEPTED,
)
async def terminate_machine(
    request: Request,
    machine_id: UUID,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
    payload: TerminateRequest | None = None,
) -> MachineReleased:
    """Release the machine: ``409 machine_has_chats`` while it serves any,
    unless ``force``. The answer names the orgs whose dedicated assignment the
    release dropped (they place by the ordinary rules again) and says where
    the machine's chats went — moved to another box, or stranded on it."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="terminate",
        resource_id=str(machine_id),
    )
    alloc = await _machine(db, machine_id, lock=True)
    try:
        released = await provisioning.terminate(
            db, alloc, caller=caller, force=(payload or TerminateRequest()).force, acting=ctx
        )
    except provisioning.ProvisionError as exc:
        raise _refusal(exc) from exc
    row = await _alloc_row(db, alloc)
    return MachineReleased(
        **row.model_dump(),
        released_orgs=[OrgRef(id=str(org.id), name=org.name) for org in released.released_orgs],
        chats_moved=released.chats_moved,
    )


@router.post("/machines", response_model=MachineMinted, status_code=status.HTTP_201_CREATED)
async def mint_platform_machine(
    request: Request,
    payload: MachineMintRequest,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> MachineMinted:
    """Mint the credential a hand-provisioned box boots with. The raw secret
    is in this answer only."""
    await _decide(
        request, db, ctx, caller, action=Action.ADMIN, operation="mint", resource_id="machines"
    )
    machine_type = await compute_service.get_machine_type_by_code(
        db, provider=payload.provider, code=payload.instance_type
    )
    if machine_type is None or not machine_type.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine type not found")
    try:
        credential, raw = await machine_credential_service.mint(
            db,
            # The platform's pool box is owned by the org the staff credential
            # is in: the request's org, as everywhere else.
            org_id=ctx.org_id,
            created_by=caller.id,
            machine_type=machine_type,
            tenancy=payload.tenancy,
            label=payload.label,
            region=payload.region,
        )
    except ValueError as exc:
        log.warning(
            "admin.machines.mint_refused",
            org_id=str(ctx.org_id),
            machine_type_id=str(machine_type.id),
            tenancy=payload.tenancy,
            error=scrub_text(str(exc)),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": MACHINE_MINT_REFUSED,
                "message": "This machine type and tenancy cannot be minted.",
            },
        ) from exc
    await db.commit()
    return MachineMinted(credential=raw, machine=await _read(db, credential))


@router.delete("/machines/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_platform_machine(
    request: Request,
    credential_id: UUID,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> None:
    """Revoke a box's credential. Its next claim or heartbeat on it is refused;
    an org it was dedicated to returns to the pool."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation="revoke",
        resource_id=str(credential_id),
    )
    credential = await machine_credential_service.revoke(db, credential_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")
    if credential.machine_id is not None:
        alloc = await db.get(ComputeAllocation, credential.machine_id)
        if alloc is not None:
            await box_revoked(db, alloc)
    await db.commit()


async def _assignment_read(db: AsyncSession, org_id: UUID) -> OrgComputeAssignmentRead:
    """The org's granted pool machine, read the way the dedicated-box card
    reads it: the box behind it and whether chats may fall back to the
    shared machines. A box the provisioning path dedicated to the org, which
    no org machine holds yet, reads the same way."""
    om = await granted_pool_machine(db, org_id)
    if om is not None and om.current_allocation_id is not None:
        settings_row = await db.get(OrgComputeSettings, org_id)
        machine_id = om.current_allocation_id
        fallback = settings_row.shared_pool_fallback if settings_row is not None else True
        assigned_at = om.created_at
    else:
        legacy = await db.get(OrgComputeAssignment, org_id)
        if legacy is None:
            return OrgComputeAssignmentRead(org_team_id=str(org_id))
        machine_id, fallback, assigned_at = (
            legacy.machine_id,
            legacy.fallback_to_pool,
            legacy.assigned_at,
        )
    # A rotated box has held more than one credential; the live one speaks
    # for it (a claim revokes the ones before it), the newest when none is.
    credential = (
        await db.execute(
            select(MachineCredential)
            .where(MachineCredential.machine_id == machine_id)
            .order_by(
                MachineCredential.revoked_at.is_(None).desc(),
                MachineCredential.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return OrgComputeAssignmentRead(
        org_team_id=str(org_id),
        machine_id=str(machine_id),
        fallback_to_pool=fallback,
        machine=await _read(db, credential) if credential is not None else None,
        assigned_at=assigned_at,
    )


@router.get("/orgs/{org_id}/compute/dedicated", response_model=OrgComputeAssignmentRead)
async def get_org_dedicated_compute(
    request: Request, org_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> OrgComputeAssignmentRead:
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.READ,
        operation="assignment",
        resource_id=str(org_id),
        org_exists=await _org_exists(db, org_id),
    )
    return await _assignment_read(db, org_id)


@router.put("/orgs/{org_id}/compute/dedicated", response_model=OrgComputeAssignmentRead)
async def set_org_dedicated_compute(
    request: Request,
    org_id: UUID,
    payload: OrgComputeAssignmentUpdate,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> OrgComputeAssignmentRead:
    """Hold one hand-provisioned dedicated box as the org's granted pool
    machine, or let it go (``machine_id: null``).

    The box must be a ``dedicated`` platform machine that is not past its
    life and has not served another org (``409``): a dedicated box serves one
    org for its whole life. The org's existing granted pool machine is pointed
    at it, or one is created, free for a year, so nothing stops at deploy.
    """
    operation = "assign" if payload.machine_id else "unassign"
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        operation=operation,
        resource_id=str(org_id),
        org_exists=await _org_exists(db, org_id),
    )
    if payload.machine_id is None:
        await unassign_dedicated(db, org_id=org_id)
        return await _assignment_read(db, org_id)
    try:
        machine_id = UUID(payload.machine_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
        ) from exc
    alloc = await _machine(db, machine_id, lock=True)
    held = await _org_machine_of(db, alloc)
    if held is not None and held.org_team_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This machine is dedicated to another organization",
        )
    try:
        await assign_dedicated(
            db,
            org_id=org_id,
            alloc=alloc,
            fallback_to_pool=payload.fallback_to_pool,
            caller=caller,
        )
    except OfferingError as exc:
        raise offering_refusal(exc) from exc
    return await _assignment_read(db, org_id)


# ---- machines an org holds, from the platform console ------------------------------


@router.get("/orgs/{org_id}/machines", response_model=list[OrgMachineRead])
async def list_org_machines(
    request: Request, org_id: UUID, db: DbSession, caller: CurrentUser, ctx: CurrentPrincipal
) -> list[OrgMachineRead]:
    """The machines the org holds, bought or given, live ones only."""
    await decide_offering(
        request,
        db,
        ctx,
        caller,
        operation=offering_policy.ORG_MACHINES,
        resource_id=str(org_id),
        org_exists=await _org_exists(db, org_id),
    )
    return await platform_org_machine_reads(db, org_id=org_id)


@router.post(
    "/orgs/{org_id}/machines",
    response_model=OrgMachineRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def grant_org_machine(
    request: Request,
    org_id: UUID,
    payload: OrgMachineGrant,
    db: DbSession,
    caller: CurrentUser,
    ctx: CurrentPrincipal,
) -> OrgMachineRead:
    """Give the org a machine, free until ``free_until``. Answers once the
    machine is recorded; it starts in the background."""
    await decide_offering(
        request,
        db,
        ctx,
        caller,
        operation=offering_policy.GRANT,
        resource_id=str(org_id),
        org_exists=await _org_exists(db, org_id),
    )
    try:
        om = await grant_machine(db, org_id=org_id, payload=payload, caller=caller)
    except OfferingError as exc:
        raise offering_refusal(exc) from exc
    await record_org_audit(
        db,
        org_id=org_id,
        actor=caller,
        action="machine.granted",
        target=str(om.id),
        detail={
            "offering_id": str(om.offering_id),
            "name": om.name,
            "use_mode": om.use_mode,
            "free_until": payload.free_until.isoformat(),
        },
        acting=ctx,
    )
    (read,) = await platform_org_machine_reads(db, org_id=org_id, machine_ids=[om.id])
    return read
