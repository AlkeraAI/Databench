"""The machines Alkera sells (offerings), and the machines it gives orgs.

An offering is a machine type (the provider's hardware shape) sold under a
name, at a price rule (``pass_through``: the provider's price plus a markup;
``fixed``: a set rate), with storage options, to an audience (every org,
enterprise orgs, or the orgs listed on it). Platform admins write offerings;
an offering is retired, never deleted, because org machines and invoices keep
naming it.

An org sees an offering when it is live (not retired, purchasable), its
machine type is active and has stock for new machines, this deployment can
start machines at its provider, and the org is in its audience. The provider
check is a predicate the caller may substitute (the default asks the provider
registry), because whether a provider is configured is the deployment's
environment, not the database's.

Granting gives an org a machine free until a date: an ``org_machines`` row
with ``acquisition='granted'``, created through the same primitive a purchase
uses, so the reconcile starts it like any other.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from alkera_core.compute.billing_port import compute_billing
from alkera_core.compute.disk import (
    DiskChoices,
    DiskSpecError,
    check_offering_bounds,
    disk_rules_for,
)
from alkera_core.compute.meter import machines_are_priced
from alkera_core.compute.offering_disks import disk_choices_for, storage_refusal
from alkera_core.compute.org_machines import AdmittedRate, create_org_machine, free_machine_name
from alkera_core.compute.pricing import compute_rate
from alkera_core.compute.provider import UnknownComputeProviderError, provider_for
from alkera_core.compute.stock import Stock
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.models import OrgComputeAssignment, Team, TeamMembership, User
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    DEDICATED_TENANCY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute_offerings import (
    AUDIENCE_ALL,
    AUDIENCE_ENTERPRISE,
    AUDIENCE_LISTED,
    PRICING_FIXED,
    ComputeOffering,
    ComputeOfferingOrg,
)
from alkera_core.models.org_machines import (
    GRANTED,
    POWER_OFF,
    USE_POOL,
    OrgComputeSettings,
    OrgMachine,
)
from alkera_core.schemas.org_machines import (
    AudienceGrant,
    DiskChoicesRead,
    GpuSpec,
    OfferingAdminRead,
    OfferingCreate,
    OfferingRead,
    OfferingUpdate,
    OrgMachineGrant,
)
from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute.offering_stock import live_answers, stock_read, verdict
from backend.services.org import ancestor_chain

#: Whether this deployment can start machines of a type at its provider.
ProviderConfigured = Callable[[ComputeMachineType], bool]

#: How long a dedicated box handed to an org through the old assignment route
#: stays free: long enough that nothing stops at deploy, as the back-fill did.
CARRIED_OVER_FREE_DAYS = 365


class OfferingError(Exception):
    """A refused catalog or grant write; the route answers ``status`` with
    ``{code, message}``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def provider_configured(machine_type: ComputeMachineType) -> bool:
    """Whether this deployment holds what the type's provider needs to start a
    machine. A provider the registry does not know is not configured."""
    try:
        return provider_for(machine_type, settings).configured()
    except UnknownComputeProviderError:
        return False


@dataclass(frozen=True, slots=True)
class PricedOffering:
    offering: ComputeOffering
    machine_type: ComputeMachineType


def _gpu(machine_type: ComputeMachineType) -> GpuSpec | None:
    if machine_type.gpu_count <= 0:
        return None
    return GpuSpec(
        name=machine_type.gpu_name,
        count=machine_type.gpu_count,
        memory_gb=machine_type.gpu_memory_gb,
    )


def _public_fields(
    offering: ComputeOffering, machine_type: ComputeMachineType, stock: Stock | None = None
) -> dict[str, object]:
    return {
        "id": str(offering.id),
        "name": offering.name,
        "description": offering.description,
        "provider": machine_type.provider,
        "region": offering.region,
        "compute_class": machine_type.compute_class,
        "gpu": _gpu(machine_type),
        "vcpu": machine_type.vcpu,
        "memory_gb": machine_type.memory_gb,
        "rate_per_minute_nanos": compute_rate(offering, machine_type),
        "storage_rate_per_gb_month_nanos": offering.storage_rate_per_gb_month_nanos,
        "storage_gb_default": offering.storage_gb_default,
        "storage_gb_max": offering.storage_gb_max,
        "availability": machine_type.availability,
        "purchasable": offering.purchasable,
        "idle_stop_minutes_default": offering.idle_stop_minutes_default,
        "start_runway_minutes": settings.machine_start_runway_minutes,
        "priced": machines_are_priced(),
        **_disk_fields(offering, machine_type),
        "stock": stock_read(stock if stock is not None else verdict(machine_type)),
    }


def disk_choices_read(choices: DiskChoices) -> DiskChoicesRead:
    return DiskChoicesRead(
        volume_min_gb=choices.volume.min_gb,
        volume_max_gb=choices.volume.max_gb,
        volume_default_gb=choices.volume.default_gb,
        container_gb=choices.container_gb,
        volume_billed_while_stopped=choices.volume_billed_while_stopped,
        grow=choices.grow.value,
    )


def _disk_fields(offering: ComputeOffering, machine_type: ComputeMachineType) -> dict[str, object]:
    """The offering's disk choices; one its provider cannot make offers none
    and cannot be bought."""
    try:
        choices = disk_choices_for(offering, machine_type)
    except DiskSpecError:
        return {"disk": None, "purchasable": False}
    return {"disk": disk_choices_read(choices)}


def _check_disks(offering: ComputeOffering, machine_type: ComputeMachineType) -> None:
    try:
        check_offering_bounds(
            storage_gb_default=offering.storage_gb_default,
            storage_gb_max=offering.storage_gb_max,
            rules=disk_rules_for(machine_type.provider, machine_type.compute_class),
        )
    except DiskSpecError as exc:
        raise OfferingError(422, "storage_outside_provider", str(exc)) from exc


def offering_read(
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    *,
    rate_per_minute_nanos: int | None = None,
    stock: Stock | None = None,
) -> OfferingRead:
    """The offering as an org's buyer sees it: the customer rate, no cost.
    ``rate_per_minute_nanos`` replaces the offering's own rate with the one
    the buyer's org would actually pin (a negotiated grant's)."""
    fields = _public_fields(offering, machine_type, stock)
    if rate_per_minute_nanos is not None:
        fields["rate_per_minute_nanos"] = rate_per_minute_nanos
    return OfferingRead.model_validate(fields)


def offering_admin_read(
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    org_ids: Sequence[UUID],
    stock: Stock | None = None,
) -> OfferingAdminRead:
    """As a buyer reads it (the same stock verdict, so staff see what customers
    see) and with what the platform sets."""
    return OfferingAdminRead.model_validate(
        {
            **_public_fields(offering, machine_type, stock),
            "machine_type_id": str(offering.machine_type_id),
            "pricing_mode": offering.pricing_mode,
            "markup_bps": offering.markup_bps,
            "fixed_rate_per_minute_nanos": offering.fixed_rate_per_minute_nanos,
            "provider_price_per_minute_nanos": machine_type.provider_price_per_minute_nanos,
            "audience": offering.audience,
            "org_ids": sorted(str(org_id) for org_id in org_ids),
            "sort_order": offering.sort_order,
            "retired_at": offering.retired_at,
        }
    )


async def _org_ids_of(db: AsyncSession, offering_ids: Sequence[UUID]) -> dict[UUID, list[UUID]]:
    out: dict[UUID, list[UUID]] = {offering_id: [] for offering_id in offering_ids}
    if not offering_ids:
        return out
    rows = await db.execute(
        select(ComputeOfferingOrg.offering_id, ComputeOfferingOrg.org_team_id).where(
            ComputeOfferingOrg.offering_id.in_(offering_ids)
        )
    )
    for offering_id, org_id in rows.all():
        out[offering_id].append(org_id)
    return out


async def admin_offerings(db: AsyncSession) -> list[OfferingAdminRead]:
    """Every offering, retired ones included, with the provider's live price
    and the customer rate it yields."""
    rows = (
        await db.execute(
            select(ComputeOffering, ComputeMachineType)
            .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
            .order_by(ComputeOffering.sort_order, ComputeOffering.name, ComputeOffering.id)
        )
    ).all()
    orgs = await _org_ids_of(db, [offering.id for offering, _ in rows])
    live = await live_answers([mt for _, mt in rows])
    return [
        offering_admin_read(offering, mt, orgs[offering.id], verdict(mt, live))
        for offering, mt in rows
    ]


async def admin_offering(db: AsyncSession, offering_id: UUID) -> OfferingAdminRead:
    offering = await _get(db, offering_id)
    machine_type = await _machine_type(db, offering.machine_type_id)
    orgs = await _org_ids_of(db, [offering.id])
    live = await live_answers([machine_type])
    return offering_admin_read(
        offering, machine_type, orgs[offering.id], verdict(machine_type, live)
    )


async def is_enterprise(db: AsyncSession, org_id: UUID) -> bool:
    """An enterprise org holds an ``enterprise_plans`` row; on a self-hosted
    deployment every org is one."""
    if settings.is_self_hosted:
        return True
    return await compute_billing().holds_enterprise_plan(db, org_id)


async def visible_offerings(
    db: AsyncSession,
    org_id: UUID,
    *,
    configured: ProviderConfigured | None = None,
) -> list[PricedOffering]:
    """The offerings ``org_id`` may buy now, in catalog order."""
    enterprise = await is_enterprise(db, org_id)
    audience = [
        ComputeOffering.audience == AUDIENCE_ALL,
        and_(
            ComputeOffering.audience == AUDIENCE_LISTED,
            exists().where(
                ComputeOfferingOrg.offering_id == ComputeOffering.id,
                ComputeOfferingOrg.org_team_id == org_id,
            ),
        ),
    ]
    if enterprise:
        audience.append(ComputeOffering.audience == AUDIENCE_ENTERPRISE)
    rows = (
        await db.execute(
            select(ComputeOffering, ComputeMachineType)
            .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
            .where(
                ComputeOffering.retired_at.is_(None),
                ComputeOffering.purchasable.is_(True),
                ComputeMachineType.active.is_(True),
                ComputeMachineType.available_for_new.is_(True),
                or_(*audience),
            )
            .order_by(ComputeOffering.sort_order, ComputeOffering.name, ComputeOffering.id)
        )
    ).all()
    return [PricedOffering(offering, mt) for offering, mt in rows if can_offer(mt, configured)]


# ---- catalog writes -----------------------------------------------------------------


async def _get(db: AsyncSession, offering_id: UUID, *, lock: bool = False) -> ComputeOffering:
    query = select(ComputeOffering).where(ComputeOffering.id == offering_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    offering = (await db.execute(query)).scalar_one_or_none()
    if offering is None:
        raise OfferingError(404, "offering_not_found", "Offering not found")
    return offering


async def _machine_type(db: AsyncSession, machine_type_id: UUID) -> ComputeMachineType:
    machine_type = await db.get(ComputeMachineType, machine_type_id)
    if machine_type is None:
        raise OfferingError(422, "machine_type_not_found", "Machine type not found")
    return machine_type


def _uuid(raw: str, *, code: str, message: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise OfferingError(422, code, message) from exc


async def _orgs(db: AsyncSession, raw_ids: Sequence[str]) -> list[UUID]:
    """The org ids a listed offering is sold to, each a real org root."""
    ids = list(
        dict.fromkeys(
            _uuid(raw, code="org_not_found", message="Organization not found") for raw in raw_ids
        )
    )
    if not ids:
        return []
    found = set(
        (
            await db.execute(select(Team.id).where(Team.id.in_(ids), Team.is_root.is_(True)))
        ).scalars()
    )
    if len(found) != len(ids):
        raise OfferingError(422, "org_not_found", "Organization not found")
    return ids


def _check_offering(offering: ComputeOffering, org_ids: Sequence[UUID]) -> None:
    if offering.pricing_mode == PRICING_FIXED and offering.fixed_rate_per_minute_nanos is None:
        raise OfferingError(422, "fixed_rate_required", "A fixed price offering needs a price")
    if offering.storage_gb_max < offering.storage_gb_default:
        raise OfferingError(
            422,
            "storage_max_below_default",
            "Maximum storage must be at least the default storage",
        )
    if org_ids and offering.audience != AUDIENCE_LISTED:
        raise OfferingError(
            422,
            "org_ids_need_listed",
            "Organizations can be listed only on an offering sold to listed organizations",
        )


def _resolve(configured: ProviderConfigured | None) -> ProviderConfigured:
    """The predicate a caller passed, else the deployment's own; read at call
    time so a test can stand in for the deployment at the module seam."""
    return configured if configured is not None else provider_configured


def can_offer(
    machine_type: ComputeMachineType, configured: ProviderConfigured | None = None
) -> bool:
    """Whether this deployment may sell, give or buy a machine of this type:
    it can start machines at the type's provider. The one predicate the
    catalog writes, the buy list, a purchase and the admin type list read, so
    a provider that cannot run here (a local box outside a local deployment)
    is refused everywhere at once."""
    return _resolve(configured)(machine_type)


def _check_provider(
    machine_type: ComputeMachineType, configured: ProviderConfigured | None
) -> None:
    if not can_offer(machine_type, configured):
        raise OfferingError(
            422,
            "provider_not_configured",
            f"This deployment cannot start {machine_type.provider} machines",
        )


async def _set_orgs(db: AsyncSession, offering_id: UUID, org_ids: Sequence[UUID]) -> None:
    await db.execute(
        delete(ComputeOfferingOrg).where(ComputeOfferingOrg.offering_id == offering_id)
    )
    db.add_all(ComputeOfferingOrg(offering_id=offering_id, org_team_id=org) for org in org_ids)
    await db.flush()


async def create_offering(
    db: AsyncSession,
    payload: OfferingCreate,
    *,
    created_by: UUID | None,
    configured: ProviderConfigured | None = None,
) -> ComputeOffering:
    """Add an offering. Flushes, never commits."""
    machine_type = await _machine_type(
        db,
        _uuid(
            payload.machine_type_id, code="machine_type_not_found", message="Machine type not found"
        ),
    )
    org_ids = await _orgs(db, payload.org_ids)
    offering = ComputeOffering(
        machine_type_id=machine_type.id,
        name=payload.name,
        description=payload.description,
        pricing_mode=payload.pricing_mode,
        markup_bps=payload.markup_bps,
        fixed_rate_per_minute_nanos=payload.fixed_rate_per_minute_nanos,
        storage_gb_default=payload.storage_gb_default,
        storage_gb_max=payload.storage_gb_max,
        storage_rate_per_gb_month_nanos=payload.storage_rate_per_gb_month_nanos,
        region=payload.region,
        audience=payload.audience,
        purchasable=payload.purchasable,
        idle_stop_minutes_default=payload.idle_stop_minutes_default,
        sort_order=payload.sort_order,
        created_by=created_by,
    )
    _check_offering(offering, org_ids)
    _check_disks(offering, machine_type)
    _check_provider(machine_type, configured)
    db.add(offering)
    await db.flush()
    await _set_orgs(db, offering.id, org_ids)
    return offering


#: The fields an update copies onto the row as sent.
_PLAIN_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "pricing_mode",
    "markup_bps",
    "fixed_rate_per_minute_nanos",
    "storage_gb_default",
    "storage_gb_max",
    "storage_rate_per_gb_month_nanos",
    "region",
    "audience",
    "purchasable",
    "idle_stop_minutes_default",
    "sort_order",
)
#: Fields whose column is NOT NULL: sending null for one is a 422, not a clear.
_REQUIRED_FIELDS = frozenset(_PLAIN_FIELDS) - {
    "fixed_rate_per_minute_nanos",
    "idle_stop_minutes_default",
}


async def update_offering(
    db: AsyncSession,
    offering_id: UUID,
    payload: OfferingUpdate,
    *,
    configured: ProviderConfigured | None = None,
    now: datetime | None = None,
) -> ComputeOffering:
    """Change an offering; fields not sent are left as they are. ``retired``
    true retires it (machines bought from it keep running; nobody buys it
    again), false brings it back. Flushes, never commits."""
    sent = payload.model_fields_set
    offering = await _get(db, offering_id, lock=True)
    for field in sent & _REQUIRED_FIELDS:
        if getattr(payload, field) is None:
            raise OfferingError(422, "field_required", f"{field} cannot be empty")
    if "machine_type_id" in sent and payload.machine_type_id is not None:
        machine_type_id = _uuid(
            payload.machine_type_id,
            code="machine_type_not_found",
            message="Machine type not found",
        )
        if machine_type_id != offering.machine_type_id:
            held = (
                await db.execute(select(exists().where(OrgMachine.offering_id == offering.id)))
            ).scalar_one()
            if held:
                raise OfferingError(
                    409,
                    "offering_in_use",
                    "Machines were bought from this offering; retire it and add a new one",
                )
            _check_provider(await _machine_type(db, machine_type_id), configured)
            offering.machine_type_id = machine_type_id
    for field in _PLAIN_FIELDS:
        if field in sent:
            setattr(offering, field, getattr(payload, field))
    if "org_ids" in sent and payload.org_ids is not None:
        org_ids = await _orgs(db, payload.org_ids)
    elif offering.audience == AUDIENCE_LISTED:
        org_ids = (await _org_ids_of(db, [offering.id]))[offering.id]
    else:
        # Away from listed: the list no longer means anything, so it goes.
        org_ids = []
    _check_offering(offering, org_ids)
    _check_disks(offering, await _machine_type(db, offering.machine_type_id))
    if "retired" in sent and payload.retired is not None:
        if payload.retired and offering.retired_at is None:
            offering.retired_at = now or datetime.now(UTC)
        elif not payload.retired:
            if offering.retired_at is not None:
                _check_provider(await _machine_type(db, offering.machine_type_id), configured)
            offering.retired_at = None
    await db.flush()
    await _set_orgs(db, offering.id, org_ids)
    return offering


# ---- machines Alkera gives an org ---------------------------------------------------


async def _validate_audience(
    db: AsyncSession, org_id: UUID, audience: Sequence[AudienceGrant]
) -> None:
    """Every team in the audience sits in the org, every person is a member."""
    for grant in audience:
        if grant.kind == "team":
            team_id = _uuid(
                grant.team_id or "", code="audience_not_in_org", message="Team not found"
            )
            chain = await ancestor_chain(db, team_id)
            if not chain or chain[-1].id != org_id:
                raise OfferingError(422, "audience_not_in_org", "Team not found")
        elif grant.kind == "user":
            user_id = _uuid(
                grant.user_id or "", code="audience_not_in_org", message="Member not found"
            )
            member = (
                await db.execute(
                    select(
                        exists().where(
                            TeamMembership.user_id == user_id,
                            TeamMembership.org_team_id == org_id,
                            TeamMembership.team_id == org_id,
                        )
                    )
                )
            ).scalar_one()
            if not member:
                raise OfferingError(422, "audience_not_in_org", "Member not found")
        elif grant.team_id is not None or grant.user_id is not None:
            raise OfferingError(422, "audience_invalid", "The whole organization names no team")


async def grant_machine(
    db: AsyncSession,
    *,
    org_id: UUID,
    payload: OrgMachineGrant,
    caller: User,
    configured: ProviderConfigured | None = None,
    now: datetime | None = None,
) -> OrgMachine:
    """Give ``org_id`` a machine of an offering, free until
    ``payload.free_until``. The machine starts pending; the reconcile
    provisions it. Flushes, never commits."""
    moment = now or datetime.now(UTC)
    if payload.free_until <= moment:
        raise OfferingError(422, "free_until_past", "Free until must be in the future")
    offering = await _get(
        db, _uuid(payload.offering_id, code="offering_not_found", message="Offering not found")
    )
    if offering.retired_at is not None:
        raise OfferingError(409, "offering_unavailable", "This offering is retired")
    machine_type = await _machine_type(db, offering.machine_type_id)
    if (refused := storage_refusal(offering, machine_type, payload.storage_gb)) is not None:
        raise OfferingError(422, "storage_out_of_range", refused)
    _check_provider(machine_type, configured)
    if payload.use_mode == USE_POOL and not await is_enterprise(db, org_id):
        raise OfferingError(
            422,
            "pool_needs_enterprise",
            "Only an enterprise organization's chats run on its org pool",
        )
    async with cross_tenant_write(db, reason="admin.machine_grant"):
        await _validate_audience(db, org_id, payload.audience)
        # A taken name meets the live-name index, which answers name_taken.
        om = await create_org_machine(
            db,
            org_id=org_id,
            owner_team_id=org_id,
            offering=offering,
            machine_type=machine_type,
            name=payload.name,
            acquisition=GRANTED,
            free_until=payload.free_until,
            use_mode=payload.use_mode,
            storage_gb=payload.storage_gb,
            audience=[] if payload.use_mode == USE_POOL else payload.audience,
            idle_stop_minutes=offering.idle_stop_minutes_default,
            monthly_cap_nanos=None,
            # Free until ``free_until``; past it the meter stops the machine,
            # and a start is admitted at the rate of the day.
            admitted=AdmittedRate(rate_per_minute_nanos=0, billing_account_id=None),
            created_by=caller.id,
        )
    return om


# ---- the old dedicated-box route -----------------------------------------------------


async def granted_pool_machine(db: AsyncSession, org_id: UUID) -> OrgMachine | None:
    """The org's oldest live machine Alkera gave it for its org pool."""
    return (
        await db.execute(
            select(OrgMachine)
            .where(
                OrgMachine.org_team_id == org_id,
                OrgMachine.deleted_at.is_(None),
                OrgMachine.acquisition == GRANTED,
                OrgMachine.use_mode == USE_POOL,
            )
            .order_by(OrgMachine.created_at, OrgMachine.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def carried_over_offering(
    db: AsyncSession, machine_type: ComputeMachineType, org_id: UUID, *, storage_gb: int
) -> ComputeOffering:
    """The free offering a hand-provisioned box is held under: fixed at rate
    0, sold to no one (not purchasable), listed with the orgs holding one. The
    same row the schema back-fill created per machine type, found or made."""
    offering = (
        await db.execute(
            select(ComputeOffering)
            .where(
                ComputeOffering.machine_type_id == machine_type.id,
                ComputeOffering.pricing_mode == PRICING_FIXED,
                ComputeOffering.fixed_rate_per_minute_nanos == 0,
                ComputeOffering.audience == AUDIENCE_LISTED,
                ComputeOffering.purchasable.is_(False),
            )
            .order_by(ComputeOffering.created_at, ComputeOffering.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    size = max(storage_gb, 1)
    if offering is None:
        offering = ComputeOffering(
            machine_type_id=machine_type.id,
            name=machine_type.display_name[:128],
            pricing_mode=PRICING_FIXED,
            markup_bps=0,
            fixed_rate_per_minute_nanos=0,
            storage_gb_default=size,
            storage_gb_max=size,
            storage_rate_per_gb_month_nanos=0,
            audience=AUDIENCE_LISTED,
            purchasable=False,
        )
        db.add(offering)
        await db.flush()
    elif offering.storage_gb_max < size:
        offering.storage_gb_max = size
    listed = (
        await db.execute(
            select(
                exists().where(
                    ComputeOfferingOrg.offering_id == offering.id,
                    ComputeOfferingOrg.org_team_id == org_id,
                )
            )
        )
    ).scalar_one()
    if not listed:
        db.add(ComputeOfferingOrg(offering_id=offering.id, org_team_id=org_id))
    await db.flush()
    return offering


async def _set_fallback(db: AsyncSession, org_id: UUID, fallback: bool) -> None:
    row = await db.get(OrgComputeSettings, org_id)
    if row is None:
        db.add(OrgComputeSettings(org_team_id=org_id, shared_pool_fallback=fallback))
    elif row.shared_pool_fallback != fallback:
        row.shared_pool_fallback = fallback
        row.version += 1
    await db.flush()


async def assign_dedicated(
    db: AsyncSession,
    *,
    org_id: UUID,
    alloc: ComputeAllocation,
    fallback_to_pool: bool,
    caller: User,
    now: datetime | None = None,
) -> OrgMachine:
    """Hold a hand-provisioned dedicated box as the org's granted pool
    machine: the org's existing one is pointed at it, or one is created. The
    box takes the org as its tenant for life. Flushes, never commits."""
    moment = now or datetime.now(UTC)
    if alloc.tenancy != DEDICATED_TENANCY or alloc.state in COMPUTE_TERMINAL_STATES:
        raise OfferingError(404, "machine_not_found", "Machine not found")
    if alloc.tenant_org_id is not None and alloc.tenant_org_id != org_id:
        raise OfferingError(
            409,
            "machine_held_another_org",
            "This machine has served another organization; provision a new one for this "
            "organization",
        )
    holder = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.current_allocation_id == alloc.id, OrgMachine.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    om = holder or await granted_pool_machine(db, org_id)
    machine_type = await _machine_type(db, alloc.machine_type_id)
    alloc.tenant_org_id = org_id
    await db.flush()
    if om is None:
        offering = await carried_over_offering(
            db, machine_type, org_id, storage_gb=alloc.storage_gb
        )
        om = OrgMachine(
            org_team_id=org_id,
            owner_team_id=org_id,
            offering_id=offering.id,
            name=await free_machine_name(
                db, org_id=org_id, wanted=(alloc.name or "").strip() or "Machine 1"
            ),
            acquisition=GRANTED,
            free_until=moment + timedelta(days=CARRIED_OVER_FREE_DAYS),
            use_mode=USE_POOL,
            storage_gb=max(alloc.storage_gb, 1),
            created_by=caller.id,
        )
        db.add(om)
        await db.flush()
    elif om.current_allocation_id != alloc.id:
        previous = (
            await db.get(ComputeAllocation, om.current_allocation_id)
            if om.current_allocation_id is not None
            else None
        )
        if previous is not None and previous.org_machine_id == om.id:
            previous.org_machine_id = None
        om.current_allocation_id = None
        om.version += 1
        await db.flush()
    om.current_allocation_id = alloc.id
    alloc.org_machine_id = om.id
    await _set_fallback(db, org_id, fallback_to_pool)
    await _forget_legacy_assignment(db, org_id=org_id)
    await db.flush()
    return om


async def _forget_legacy_assignment(
    db: AsyncSession, *, org_id: UUID | None = None, machine_id: UUID | None = None
) -> None:
    """Drop the one-box-per-org row the provisioning path still writes, for
    the org or the box: an org's machines are its org machines now, and a
    stale row would keep pointing its chats at a box it let go of."""
    query = delete(OrgComputeAssignment)
    if org_id is not None:
        query = query.where(OrgComputeAssignment.org_team_id == org_id)
    if machine_id is not None:
        query = query.where(OrgComputeAssignment.machine_id == machine_id)
    await db.execute(query)


async def legacy_assignment_org(db: AsyncSession, alloc: ComputeAllocation) -> UUID | None:
    """The org a box was provisioned for through the one-box-per-org row, when
    no org machine holds it yet."""
    return (
        await db.execute(
            select(OrgComputeAssignment.org_team_id).where(
                OrgComputeAssignment.machine_id == alloc.id
            )
        )
    ).scalar_one_or_none()


async def let_go_of_box(db: AsyncSession, om: OrgMachine, *, now: datetime | None = None) -> None:
    """``om`` lets go of the box behind it and is deleted. The box itself is
    left standing for the platform to decide on; it keeps the org as tenant,
    so it never serves another. Flushes, never commits."""
    if om.current_allocation_id is not None:
        alloc = await db.get(ComputeAllocation, om.current_allocation_id)
        if alloc is not None and alloc.org_machine_id == om.id:
            alloc.org_machine_id = None
    om.current_allocation_id = None
    om.deleted_at = now or datetime.now(UTC)
    om.desired_power = POWER_OFF
    om.version += 1
    await db.flush()


async def unassign_dedicated(
    db: AsyncSession, *, org_id: UUID, now: datetime | None = None
) -> None:
    """Return the org's chats to the shared machines: its granted pool machine
    lets go of the box and is deleted."""
    om = await granted_pool_machine(db, org_id)
    if om is not None:
        await let_go_of_box(db, om, now=now)
    await _forget_legacy_assignment(db, org_id=org_id)


async def box_revoked(db: AsyncSession, alloc: ComputeAllocation) -> None:
    """The box's credential was taken away. A granted pool machine it backed
    (a hand-provisioned box held for an org) lets go of it, and the org's
    chats return to the shared machines. Any other org machine keeps its
    claim; the box goes off the plane and the machine is replaced like any
    lost one."""
    await _forget_legacy_assignment(db, machine_id=alloc.id)
    if alloc.org_machine_id is None:
        return
    om = await db.get(OrgMachine, alloc.org_machine_id)
    if (
        om is not None
        and om.deleted_at is None
        and om.current_allocation_id == alloc.id
        and om.acquisition == GRANTED
        and om.use_mode == USE_POOL
    ):
        await let_go_of_box(db, om)


__all__ = [
    "CARRIED_OVER_FREE_DAYS",
    "OfferingError",
    "PricedOffering",
    "ProviderConfigured",
    "admin_offering",
    "admin_offerings",
    "assign_dedicated",
    "box_revoked",
    "can_offer",
    "carried_over_offering",
    "create_offering",
    "grant_machine",
    "granted_pool_machine",
    "is_enterprise",
    "legacy_assignment_org",
    "let_go_of_box",
    "offering_admin_read",
    "offering_read",
    "provider_configured",
    "unassign_dedicated",
    "update_offering",
    "visible_offerings",
]
