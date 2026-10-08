"""Buying an org machine: what the org may see and buy, the quota admission
reads, the purchase itself, and admission for a machine the org already holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.authz.policies import org_machine as policy
from alkera_core.compute.billing_port import compute_billing
from alkera_core.compute.meter import machines_are_priced
from alkera_core.compute.offering_disks import disk_choices_for, storage_refusal
from alkera_core.compute.org_machines import (
    create_org_machine,
)
from alkera_core.compute.pricing import MINUTES_PER_MONTH
from alkera_core.compute.start_runway import start_runway_nanos
from alkera_core.idempotency import (
    IdempotencyMismatch,
    KeyOwner,
    Replay,
    body_digest,
    principal_uuid,
    recorded_answer,
    register_scope,
    replay_or_claim,
    settle,
)
from alkera_core.models import (
    ComputeGrant,
    ComputeMachineType,
    TeamMembership,
)
from alkera_core.models.compute_offerings import ComputeOffering, ComputeOfferingOrg
from alkera_core.models.org_machines import (
    UNBILLED_ACQUISITIONS,
    OrgMachine,
)
from alkera_core.schemas.org_machines import (
    AudienceGrant,
    MachineBuyingRead,
    MachineQuote,
    MachineQuoteRequest,
    OfferingRead,
    OrgMachinePurchase,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_org_audit
from backend.services.compute import grants, offering_stock, org_admission, placement
from backend.services.compute.offerings import can_offer, offering_read, visible_offerings
from backend.services.compute.org_machine_access import (
    PURCHASED,
    MachineRow,
    OrgMachineError,
    Viewer,
    announce,
    load_row,
    name_taken,
    offering_unavailable,
    uuid_or_none,
)
from backend.services.org import descendant_ids

# --------------------------------------------------------------------------- #
# buying
# --------------------------------------------------------------------------- #

#: A purchase's idempotency key answers with the machine it bought for this
#: long. A client retries a purchase within minutes; the month is for one that
#: was offline when the answer came back.
PURCHASE_KEYS = register_scope("org_machine.purchase", retention=timedelta(days=30))


@dataclass(frozen=True, slots=True)
class PurchasePlan:
    """A purchase with every fact resolved, decided on, and ready to write."""

    owner_team_id: UUID
    offering: ComputeOffering | None
    machine_type: ComputeMachineType | None
    attrs: dict[str, object]
    quota: MachineQuota
    buyable: bool


async def offering_visible_to(db: AsyncSession, *, org_id: UUID, offering: ComputeOffering) -> bool:
    """Whether the org may see ``offering`` in its buy list at all: not
    retired, and its audience is everyone, the Enterprise plan (or a
    self-hosted deployment) or a list that names the org."""
    if offering.retired_at is not None:
        return False
    if offering.audience == "all":
        return True
    if offering.audience == "enterprise":
        return await placement.org_pool_applies(db, org_id=org_id)
    if offering.audience == "listed":
        listed = await db.execute(
            select(ComputeOfferingOrg.offering_id).where(
                ComputeOfferingOrg.offering_id == offering.id,
                ComputeOfferingOrg.org_team_id == org_id,
            )
        )
        return listed.first() is not None
    return False


async def buyer_offerings(
    db: AsyncSession, *, ctx: ActingContext, now: datetime | None = None
) -> list[OfferingRead]:
    """The offerings the caller's org may buy now, each quoted at the rate a
    purchase by this caller would pin
    (:func:`~backend.services.compute.org_admission.purchase_rates`): a
    negotiated grant's rate over the offering's list price."""
    moment = now or datetime.now(UTC)
    rows = await visible_offerings(db, ctx.org_id)
    live = await offering_stock.live_answers([row.machine_type for row in rows])
    out: list[OfferingRead] = []
    for row in rows:
        rate, _storage = await org_admission.purchase_rates(
            db,
            ctx=ctx,
            offering=row.offering,
            machine_type=row.machine_type,
            storage_gb=row.offering.storage_gb_default,
            now=moment,
        )
        out.append(
            offering_read(
                row.offering,
                row.machine_type,
                rate_per_minute_nanos=rate,
                stock=offering_stock.verdict(row.machine_type, live),
            )
        )
    # What can be bought first; the catalog's order within each.
    return sorted(out, key=lambda read: not read.stock.can_buy)


async def refuse_unstocked(machine_type: ComputeMachineType) -> None:
    """Refuse (409 ``out_of_stock``, or ``no_public_address``) a size that
    cannot be bought now, asking the provider afresh: the buy dialog said so
    a moment ago, and a stale "in stock" would buy a machine that waits."""
    live = await offering_stock.live_answers([machine_type], fresh=True)
    stock = offering_stock.verdict(machine_type, live)
    if stock.can_buy:
        return
    code = "out_of_stock" if stock.state == "out_of_stock" else "no_public_address"
    raise OrgMachineError(code, stock.reason)


def buyable_now(offering: ComputeOffering, machine_type: ComputeMachineType) -> bool:
    """Whether a visible offering can be bought this minute: still for sale,
    and the hardware behind it is active and open to new machines."""
    return bool(offering.purchasable and machine_type.active and machine_type.available_for_new)


def _purchase_digest(body: OrgMachinePurchase) -> bytes:
    return body_digest(body.model_dump(mode="json", exclude={"idempotency_key"}))


def _purchase_owner(viewer: Viewer) -> KeyOwner:
    return KeyOwner(
        org_id=viewer.org_id, principal_id=principal_uuid(viewer.ctx.acting_principal.id)
    )


async def plan_purchase(db: AsyncSession, viewer: Viewer, body: OrgMachinePurchase) -> PurchasePlan:
    """Resolve the facts a purchase is decided on. The owner team defaults to
    the org root; a team outside the caller's org reads as not in the org."""
    owner_team_id = viewer.org_id
    if body.owner_team_id:
        try:
            owner_team_id = UUID(body.owner_team_id)
        except ValueError:
            owner_team_id = UUID(int=0)
    owner = await viewer.roles_on(owner_team_id)
    offering: ComputeOffering | None = None
    machine_type: ComputeMachineType | None = None
    try:
        found = (
            await db.execute(
                select(ComputeOffering, ComputeMachineType)
                .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
                .where(ComputeOffering.id == UUID(body.offering_id))
            )
        ).first()
    except ValueError:
        found = None
    if found is not None:
        offering, machine_type = found
    visible = offering is not None and await offering_visible_to(
        db, org_id=viewer.org_id, offering=offering
    )
    quota = await machine_quota(db, ctx=viewer.ctx, machine_type=machine_type)
    # A repeat of a purchase that went through holds its machine already: the
    # plan's limit is not asked of it, or the retry of the purchase that took
    # the last slot would be refused while its machine bills.
    repeat = await recorded_answer(
        db,
        PURCHASE_KEYS,
        body.idempotency_key,
        _purchase_digest(body),
        owner=_purchase_owner(viewer),
    )
    attrs: dict[str, object] = {
        "in_org": owner.in_org,
        "roles": owner.roles,
        "is_org_admin": viewer.is_org_admin,
        "email_verified": viewer.email_verified,
        "operation": policy.PURCHASE,
        "offering_visible": visible,
        "quota_left": quota.left or repeat is not None,
        "sets_pool": body.use_mode == "pool",
        "org_allows_pool": await placement.org_pool_applies(db, org_id=viewer.org_id),
    }
    return PurchasePlan(
        owner_team_id=owner_team_id,
        offering=offering,
        machine_type=machine_type,
        attrs=attrs,
        quota=quota,
        buyable=bool(
            visible
            and offering is not None
            and machine_type is not None
            and buyable_now(offering, machine_type)
            # The buy list hides an offering this deployment cannot start; one
            # named by id is refused the same way.
            and can_offer(machine_type)
        ),
    )


async def name_is_taken(
    db: AsyncSession, *, org_id: UUID, name: str, except_id: UUID | None = None
) -> bool:
    stmt = select(OrgMachine.id).where(
        OrgMachine.org_team_id == org_id,
        OrgMachine.deleted_at.is_(None),
        func.lower(OrgMachine.name) == name.strip().lower(),
    )
    if except_id is not None:
        stmt = stmt.where(OrgMachine.id != except_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def validate_audience(
    db: AsyncSession, viewer: Viewer, *, owner_team_id: UUID, audience: Sequence[AudienceGrant]
) -> list[AudienceGrant]:
    """The audience as written: each grant named once, every team and person
    in the org, and for a caller who is not an org admin, only the owner team,
    its sub-teams and their members. Anything else is refused (422) naming the
    scope, rather than dropped."""
    scope_team = viewer.org_id if viewer.is_org_admin else owner_team_id
    teams_in_scope = {scope_team, *await descendant_ids(db, scope_team)}
    seen: set[tuple[str, str | None, str | None]] = set()
    out: list[AudienceGrant] = []
    users: set[UUID] = set()
    for grant in audience:
        key = (grant.kind, grant.team_id, grant.user_id)
        if key in seen:
            continue
        seen.add(key)
        if grant.kind == "org":
            if grant.team_id or grant.user_id or scope_team != viewer.org_id:
                raise _out_of_scope()
        elif grant.kind == "team":
            if (
                grant.user_id
                or not grant.team_id
                or uuid_or_none(grant.team_id) not in teams_in_scope
            ):
                raise _out_of_scope()
        elif grant.kind == "user":
            parsed = uuid_or_none(grant.user_id)
            if grant.team_id or parsed is None:
                raise _out_of_scope()
            users.add(parsed)
        else:
            raise _out_of_scope()
        out.append(grant)
    if users:
        # Membership rows sit on the team and every team above it, so a person
        # in the scope team or anywhere below it has a row on the scope team.
        members = set(
            (
                await db.execute(
                    select(TeamMembership.user_id).where(
                        TeamMembership.team_id == scope_team,
                        TeamMembership.org_team_id == viewer.org_id,
                        TeamMembership.user_id.in_(users),
                    )
                )
            )
            .scalars()
            .all()
        )
        if users - members:
            raise _out_of_scope()
    return out


def _out_of_scope() -> OrgMachineError:
    return OrgMachineError(
        "audience_out_of_scope",
        "Who can use it may only name the team that holds the machine, "
        "its teams and their members.",
        status=422,
    )


async def purchase(
    db: AsyncSession, viewer: Viewer, body: OrgMachinePurchase, plan: PurchasePlan
) -> tuple[OrgMachine, bool]:
    """Buy the machine the plan describes, once the route has decided on it.
    ``(machine, created)``: a repeat of an earlier purchase with the same
    idempotency key answers the machine it made, and writes nothing.

    The key is claimed before anything is written and in the same transaction
    as the purchase, so a refusal below leaves the key unspent and two racing
    requests under one key buy one machine. The same key on a different
    purchase (another name, offering, size or audience) is 409
    ``idempotency.mismatch``; a repeat whose machine has since been deleted is
    410 ``machine_deleted``, never a second machine.

    Refusals, each before anything is written: an offering that cannot be
    bought now (409 ``offering_unavailable``), a storage size outside the
    offering's limits (422), an audience outside the caller's scope (422), a
    name in use (409 ``name_taken``), then admission (402, 429)."""
    try:
        claim = await replay_or_claim(
            db,
            PURCHASE_KEYS,
            body.idempotency_key,
            _purchase_digest(body),
            owner=_purchase_owner(viewer),
        )
    except IdempotencyMismatch as exc:
        raise OrgMachineError(exc.code, exc.message, status=exc.status) from exc
    if isinstance(claim, Replay):
        row = await load_row(db, org_id=viewer.org_id, machine_id=str(claim.answer["machine_id"]))
        if row is None:
            raise OrgMachineError(
                "machine_deleted",
                "The machine this request bought has since been deleted.",
                status=410,
            )
        return row.machine, False
    offering, machine_type = plan.offering, plan.machine_type
    if not plan.buyable or offering is None or machine_type is None:
        raise offering_unavailable()
    await refuse_unstocked(machine_type)
    if (refused := storage_refusal(offering, machine_type, body.storage_gb)) is not None:
        raise OrgMachineError("storage_out_of_range", refused, status=422)
    audience = await validate_audience(
        db, viewer, owner_team_id=plan.owner_team_id, audience=body.audience
    )
    if await name_is_taken(db, org_id=viewer.org_id, name=body.name):
        raise name_taken()
    admission = await org_admission.admit_org_machine(
        db,
        ctx=viewer.ctx,
        org_id=viewer.org_id,
        owner_team_id=plan.owner_team_id,
        offering=offering,
        machine_type=machine_type,
        storage_gb=body.storage_gb,
    )
    # A name taken between the check and this insert meets the live-name index,
    # which answers with the same name_taken refusal the check gives.
    machine = await create_org_machine(
        db,
        org_id=viewer.org_id,
        owner_team_id=plan.owner_team_id,
        offering=offering,
        machine_type=machine_type,
        name=body.name,
        acquisition="purchased",
        free_until=None,
        use_mode=body.use_mode,
        storage_gb=body.storage_gb,
        audience=audience,
        idle_stop_minutes=body.idle_stop_minutes,
        monthly_cap_nanos=body.monthly_cap_nanos,
        admitted=admission.admitted,
        created_by=viewer.user.id,
    )
    await settle(db, claim, {"machine_id": str(machine.id)})
    await record_org_audit(
        db,
        org_id=viewer.org_id,
        actor=viewer.user,
        action=PURCHASED,
        target=str(machine.id),
        detail={
            "idempotency_key": body.idempotency_key,
            "name": machine.name,
            "offering_id": str(offering.id),
            "owner_team_id": str(plan.owner_team_id),
            "use_mode": machine.use_mode,
            "storage_gb": machine.storage_gb,
        },
        acting=viewer.ctx,
    )
    await announce(db, machine, actor=viewer.ctx.audit_dict())
    return machine, True


@dataclass(frozen=True, slots=True)
class MachineQuota:
    """How many machines the org holds against how many it may hold: the
    ceiling of the grant admission would admit a purchase under (a compute
    grant on the org, else the plan's quota)."""

    used: int
    quota: int

    @property
    def left(self) -> bool:
        return self.used < self.quota


async def machine_quota(
    db: AsyncSession, *, ctx: ActingContext, machine_type: ComputeMachineType | None
) -> MachineQuota:
    """The org's machine quota as admission reads it. Without a machine type
    (an offering that does not exist) the plan's quota stands."""
    org_id = ctx.org_id
    quota = await compute_billing().machine_quota(db, org_id)
    if machine_type is not None:
        grant = await grants.resolve_machine_grant(
            db,
            grants.MachineGrantQuery(
                ctx=ctx, org_id=org_id, machine_type=machine_type, at=datetime.now(UTC)
            ),
        )
        quota = grant.ceiling if grant is not None else 0
    used = await org_admission.held_machines(db, org_id)
    return MachineQuota(used=used, quota=quota)


async def buying(db: AsyncSession, *, ctx: ActingContext) -> MachineBuyingRead:
    """Whether the org may buy another machine: the most its plan or any live
    compute grant on the org lets it hold, against what it holds."""
    org_id = ctx.org_id
    plan = await compute_billing().machine_quota(db, org_id)
    granted = (
        await db.execute(
            select(func.max(ComputeGrant.ceiling)).where(
                ComputeGrant.org_team_id == org_id,
                ComputeGrant.expires_at > datetime.now(UTC),
            )
        )
    ).scalar_one()
    quota = max(plan, int(granted or 0))
    used = (await machine_quota(db, ctx=ctx, machine_type=None)).used
    reason: Literal["plan", "quota"] | None = None
    if quota == 0:
        reason = "plan"
    elif used >= quota:
        reason = "quota"
    return MachineBuyingRead(can_buy=reason is None, reason=reason, quota=quota, used=used)


async def admit_start(
    db: AsyncSession, *, ctx: ActingContext, row: MachineRow
) -> org_admission.Admission:
    """Admission for turning on, waking or replacing a machine the org already
    holds: the paying account must carry the start runway at the rates the
    machine will pin. The plan's quota is not asked (the org holds it).
    Raises :class:`~backend.services.compute.grants.ComputeRefusedError`.

    A machine the org runs itself is admitted with nothing asked: no grant,
    no credit, and every rate zero."""
    if row.machine.org_team_id != ctx.org_id:
        raise org_admission.ForeignOrgError
    if row.machine.acquisition in UNBILLED_ACQUISITIONS:
        return org_admission.Admission(
            billing_account_id=None, rate_per_minute_nanos=0, storage_rate_per_minute_nanos=0
        )
    return await org_admission.admit_org_machine_start(
        db,
        ctx=ctx,
        org_machine=row.machine,
        offering=row.offering,
        machine_type=row.machine_type,
    )


__all__ = [
    "PURCHASE_KEYS",
    "MachineQuota",
    "PurchasePlan",
    "admission_quote",
    "admit_start",
    "buyable_now",
    "buying",
    "machine_quota",
    "name_is_taken",
    "offering_visible_to",
    "plan_purchase",
    "purchase",
    "validate_audience",
]


async def quote(db: AsyncSession, viewer: Viewer, body: MachineQuoteRequest) -> MachineQuote:
    """What buying an offering at ``body.storage_gb`` would cost the caller's
    org and whether it would be admitted now, by the purchase's own rules: the
    admission runs in a savepoint that is always rolled back, and a refusal it
    reads is not recorded. 404 for an offering the org cannot see, 422 for a
    size the offering does not sell."""
    offering_id = uuid_or_none(body.offering_id)
    found = (
        (
            await db.execute(
                select(ComputeOffering, ComputeMachineType)
                .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
                .where(ComputeOffering.id == offering_id)
            )
        ).first()
        if offering_id is not None
        else None
    )
    if found is None or not await offering_visible_to(db, org_id=viewer.org_id, offering=found[0]):
        raise OrgMachineError("offering_not_found", "Machine type not found.", status=404)
    offering, machine_type = found
    if not buyable_now(offering, machine_type):
        raise offering_unavailable()
    await refuse_unstocked(machine_type)
    if (refused := storage_refusal(offering, machine_type, body.storage_gb)) is not None:
        raise OrgMachineError("storage_out_of_range", refused, status=422)
    return await admission_quote(
        db,
        viewer.ctx,
        org_id=viewer.org_id,
        owner_team_id=viewer.org_id,
        offering=offering,
        machine_type=machine_type,
        storage_gb=body.storage_gb,
    )


async def admission_quote(
    db: AsyncSession,
    ctx: ActingContext,
    *,
    org_id: UUID,
    owner_team_id: UUID,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    storage_gb: int,
    org_machine: OrgMachine | None = None,
) -> MachineQuote:
    """The quote at ``storage_gb``: a purchase (``org_machine`` ``None``) or a
    machine the org holds at a new size, admitted by the same rules in a
    savepoint that is always rolled back. A refusal it reads is not recorded."""
    choices = disk_choices_for(offering, machine_type)
    verdict: Literal["ok", "refused"] = "ok"
    code: str | None = None
    message = ""
    savepoint = await db.begin_nested()
    try:
        admitted = await org_admission.admit_org_machine(
            db,
            ctx=ctx,
            org_id=org_id,
            owner_team_id=owner_team_id,
            offering=offering,
            machine_type=machine_type,
            storage_gb=storage_gb,
            org_machine=org_machine,
            record_refusals=False,
        )
        rate, storage = admitted.rate_per_minute_nanos, admitted.storage_rate_per_minute_nanos
    except grants.ComputeRefusedError as exc:
        verdict, code, message = "refused", exc.code, str(exc)
        rate, storage = (
            await org_admission.purchase_rates(
                db, ctx=ctx, offering=offering, machine_type=machine_type, storage_gb=storage_gb
            )
            if org_machine is None
            else await org_admission.next_start_rates(
                db,
                ctx=ctx,
                org_machine=org_machine,
                offering=offering,
                machine_type=machine_type,
                storage_gb=storage_gb,
            )
        )
    finally:
        await savepoint.rollback()
    return MachineQuote(
        offering_id=str(offering.id),
        storage_gb=storage_gb,
        rate_per_minute_nanos=rate,
        storage_rate_per_minute_nanos=storage,
        storage_per_month_nanos=storage * MINUTES_PER_MONTH,
        volume_billed_while_stopped=choices.volume_billed_while_stopped,
        start_runway_nanos=start_runway_nanos(rate, storage),
        priced=machines_are_priced(),
        verdict=verdict,
        code=code,
        message=message,
    )
