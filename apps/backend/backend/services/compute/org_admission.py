"""Whether an org may buy, start or wake a machine, at what rates, and who pays.

:func:`admit_org_machine` is the check every purchase, every manager's start
and every wake by message passes before the machine is written or powered:

* the org is the caller's own and the owning team is in it;
* a machine grant admits it: the grant sources registered in
  :mod:`backend.services.compute.grants` are asked in order (an explicit
  ``compute_grants`` row first, whose rate overrides the offering's, then the
  org's plan quota at the offering's rate), and a purchase is refused once
  the org holds as many machines as that grant allows;
* the paying account can carry ``machine_start_runway_minutes`` of compute
  plus storage at the rates the machine will pin, in the credit classes that
  may fund a machine (never free monthly or promotional credit), and the
  owning team's budget allocation has that much left.

The credit check is :func:`alkera_core.compute.start_runway.runway_shortfall`
(the compute biller's reserve of the whole runway, released at once), run
under the org's admission lock; the reconcile asks the same one before it
replaces a machine on its own. A refusal is recorded (the ``refused`` frame
and an org audit event, in a session of their own) before it is raised.

It writes nothing; the caller inserts or powers the machine in the same
transaction. A new allocation is made at :attr:`Admission.admitted`
(``new_allocation_for`` takes nothing else); a wake re-pins the sleeping one
with :func:`pin_admitted_rates`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.meter import compute_metering
from alkera_core.compute.node_reach import callback_refusal
from alkera_core.compute.org_machines import AdmittedRate, funding_account_for, runs_free
from alkera_core.compute.pricing import compute_rate, storage_rate
from alkera_core.compute.start_runway import (
    RunwayShortfall,
    runway_shortfall,
    start_runway_nanos,
)
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, advisory_key, advisory_xact_lock
from alkera_core.events import org_root_for_team
from alkera_core.models import User
from alkera_core.models.compute import ASLEEP, ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import UNBILLED_ACQUISITIONS, OrgMachine
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute import grants
from backend.services.compute.grants import ComputeRefusedError


@dataclass(frozen=True, slots=True)
class Admission:
    """An admitted purchase, start or wake: the account that pays and the
    rates to pin."""

    billing_account_id: UUID | None
    rate_per_minute_nanos: int
    storage_rate_per_minute_nanos: int

    @property
    def admitted(self) -> AdmittedRate:
        """What a new allocation for the admitted start is made at."""
        return AdmittedRate(self.rate_per_minute_nanos, self.billing_account_id)


class MachineQuotaReachedError(grants.ComputeLimitReachedError):
    """The org holds as many machines as its plan (or its grant) allows."""

    http_status = 429

    def __init__(self, *, used: int, quota: int) -> None:
        grants.ComputeRefusedError.__init__(
            self,
            "machine_quota",
            f"Your plan allows {quota} machine{'s' if quota != 1 else ''}; your org has {used}.",
        )
        self.used = used
        self.quota = quota

    def detail_extra(self) -> dict[str, object]:
        return {"quota": self.quota, "used": self.used}


class NoPublicAddressError(grants.ComputeRefusedError):
    """A rented machine could not connect back to this deployment
    (``alkera_core.compute.node_reach``): nothing is bought or started."""

    http_status = 409

    def __init__(self, message: str) -> None:
        super().__init__("no_public_address", message)


class ForeignOrgError(grants.ComputeRefusedError):
    """The org or the owning team is not the caller's. Answered as not found,
    the same as a team that does not exist."""

    http_status = 404

    def __init__(self) -> None:
        super().__init__("not_found", "Not found.")


def runway_words(minutes: int) -> str:
    """The start runway as the buy dialog says it: "10 minutes", "1 hour",
    "2 hours" (whole hours from an hour up)."""
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours = round(minutes / 60)
    return "1 hour" if hours == 1 else f"{hours} hours"


class MachineCreditError(grants.InsufficientComputeCreditError):
    """The paying account cannot carry the start runway, or the owning team's
    budget cannot. The message names the runway this deployment asks for."""

    def __init__(self, *, team_budget: bool = False) -> None:
        runway = runway_words(settings.machine_start_runway_minutes)
        grants.ComputeRefusedError.__init__(
            self,
            "insufficient_credit",
            f"The team's budget can't cover {runway} of this machine."
            if team_budget
            else f"Not enough credit to run this machine for {runway}. Add credits and try again.",
        )
        self.team_budget = team_budget


async def _lock_org_machines(db: AsyncSession, org_id: UUID) -> None:
    """Serialize every admission that counts the org's machines on that count:
    whichever grant admits a purchase, the ceiling is checked against all the
    org's machines, so two purchases under different grants must not both
    read the same count. The key is the one plan admissions already took, so
    old and new tasks exclude each other through a deploy."""
    await advisory_xact_lock(
        db,
        advisory_key("compute-admission", f"org_machines:{org_id}"),
        rank=LockRank.COMPUTE_ADMISSION,
    )


async def held_machines(db: AsyncSession, org_id: UUID) -> int:
    """How many machines the org holds against its machine quota: the ones it
    pays for, so a host the org runs itself (an unbilled acquisition) is not
    counted. The one count admission, the quota read and the buy verdict use."""
    stmt = (
        select(func.count())
        .select_from(OrgMachine)
        .where(
            OrgMachine.org_team_id == org_id,
            OrgMachine.deleted_at.is_(None),
            OrgMachine.acquisition.not_in(UNBILLED_ACQUISITIONS),
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def _funding_account(db: AsyncSession, *, org_id: UUID, owner_team_id: UUID) -> UUID | None:
    """The owner team's pool when it has one, else the org's pool: what
    :func:`~alkera_core.compute.org_machines.funding_account_for` answers for
    a machine not written yet."""
    return await compute_metering().machine_funding_account(
        db, org_id=org_id, owner_team_id=owner_team_id
    )


async def _refuse(
    db: AsyncSession,
    error: grants.ComputeRefusedError,
    *,
    ctx: ActingContext,
    machine_type: ComputeMachineType,
) -> grants.ComputeRefusedError:
    user_id = ctx.effective_user_id
    user = await db.get(User, user_id) if user_id is not None else None
    if user is None:
        return error
    return await grants.record_refusal(error, ctx=ctx, user=user, machine_type=machine_type)


def start_rates(
    org_machine: OrgMachine | None,
    *,
    grant_rate: int | None,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    storage_gb: int,
    now: datetime,
) -> tuple[int, int]:
    """The per-minute compute and storage rates a start or a wake pins now:
    nothing while a granted machine is free, else a negotiated grant's rate
    over the offering's (fixed, or the provider's price passed through), and
    the offering's storage rate. A price that moved while a machine slept
    applies from its wake; the running session that pinned the old one has
    ended."""
    if runs_free(org_machine, now):
        return 0, 0
    rate = grant_rate if grant_rate is not None else compute_rate(offering, machine_type)
    return rate, storage_rate(offering, storage_gb)


async def _quoted_rates(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_id: UUID,
    org_machine: OrgMachine | None,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    storage_gb: int,
    now: datetime | None,
) -> tuple[int, int]:
    moment = now or datetime.now(UTC)
    grant = await grants.resolve_machine_grant(
        db,
        grants.MachineGrantQuery(ctx=ctx, org_id=org_id, machine_type=machine_type, at=moment),
    )
    return start_rates(
        org_machine,
        grant_rate=grant.rate_per_minute_nanos if grant is not None else None,
        offering=offering,
        machine_type=machine_type,
        storage_gb=storage_gb,
        now=moment,
    )


async def next_start_rates(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_machine: OrgMachine,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    now: datetime | None = None,
    storage_gb: int | None = None,
) -> tuple[int, int]:
    """What the machine's next start or wake would pin, read the way
    admission pins it (the same grant resolution and :func:`start_rates`), so
    a stopped machine's card and its wake cannot disagree; at ``storage_gb``
    when its disk is quoted bigger. Holds nothing and takes no lock."""
    return await _quoted_rates(
        db,
        ctx=ctx,
        org_id=org_machine.org_team_id,
        org_machine=org_machine,
        offering=offering,
        machine_type=machine_type,
        storage_gb=org_machine.storage_gb if storage_gb is None else storage_gb,
        now=now,
    )


async def purchase_rates(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    storage_gb: int,
    now: datetime | None = None,
) -> tuple[int, int]:
    """What buying ``offering`` for the caller's org would pin, by the same
    rule a purchase is admitted on: a negotiated grant's rate over the
    offering's. The buy dialog quotes this, so it never shows a list price
    the org will not pay. Holds nothing and takes no lock."""
    return await _quoted_rates(
        db,
        ctx=ctx,
        org_id=ctx.org_id,
        org_machine=None,
        offering=offering,
        machine_type=machine_type,
        storage_gb=storage_gb,
        now=now,
    )


async def admit_org_machine(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_id: UUID,
    owner_team_id: UUID,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    storage_gb: int,
    org_machine: OrgMachine | None = None,
    now: datetime | None = None,
    record_refusals: bool = True,
) -> Admission:
    """Admit a purchase (``org_machine`` is ``None``), or a start or wake of
    ``org_machine``.

    Refuses with :class:`ForeignOrgError` when the org is not the caller's or
    the owning team is not in it, :class:`MachineQuotaReachedError` (code
    ``machine_quota``, 429) when a purchase would pass the grant's ceiling,
    and :class:`MachineCreditError` (code ``insufficient_credit``, 402) when
    the start runway cannot be carried. A start or wake is never refused for
    quota: the org already holds the machine. Nothing stays held afterwards.
    A quote asks with ``record_refusals`` off: a refusal it reads is not one
    anybody met.
    """
    moment = now or datetime.now(UTC)

    async def refuse(error: grants.ComputeRefusedError) -> grants.ComputeRefusedError:
        if not record_refusals:
            return error
        return await _refuse(db, error, ctx=ctx, machine_type=machine_type)

    if org_id != ctx.org_id or await org_root_for_team(db, owner_team_id) != org_id:
        raise ForeignOrgError
    if org_machine is not None and org_machine.org_team_id != org_id:
        raise ForeignOrgError
    if offering.machine_type_id != machine_type.id:
        raise ValueError("the machine type is not the offering's")
    if (unreachable := callback_refusal(machine_type.provider, settings.node_api_url)) is not None:
        raise NoPublicAddressError(unreachable)

    grant = await grants.resolve_machine_grant(
        db,
        grants.MachineGrantQuery(ctx=ctx, org_id=org_id, machine_type=machine_type, at=moment),
    )
    if grant is None:
        raise await refuse(grants.NoComputeGrantError())
    await _lock_org_machines(db, org_id)

    if org_machine is None:
        used = await held_machines(db, org_id)
        if used >= grant.ceiling:
            raise await refuse(MachineQuotaReachedError(used=used, quota=grant.ceiling))

    rate, storage = start_rates(
        org_machine,
        grant_rate=grant.rate_per_minute_nanos,
        offering=offering,
        machine_type=machine_type,
        storage_gb=storage_gb,
        now=moment,
    )

    account = (
        await funding_account_for(db, org_machine)
        if org_machine is not None
        else await _funding_account(db, org_id=org_id, owner_team_id=owner_team_id)
    )
    target = org_machine.id if org_machine is not None else "new"
    short = await runway_shortfall(
        db,
        account_id=account,
        owner_team_id=owner_team_id,
        org_id=org_id,
        need_nanos=start_runway_nanos(rate, storage),
        request_id=f"compute-admit:{target}:{uuid.uuid4()}",
        now=moment,
    )
    if short is not None:
        raise await refuse(MachineCreditError(team_budget=short == RunwayShortfall.TEAM_BUDGET))
    return Admission(
        billing_account_id=account,
        rate_per_minute_nanos=rate,
        storage_rate_per_minute_nanos=storage,
    )


async def admit_org_machine_start(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_machine: OrgMachine,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    now: datetime | None = None,
) -> Admission:
    """Admit a start, a wake or a replacement of a machine the org already
    holds: the credit check alone, never the quota (the org holds the machine
    whatever its plan allows now). Both prove today's rates; a wake re-pins
    them on the sleeping allocation, so the session it starts is billed at
    the rate the offering (or a negotiated grant) carries now. A start on fresh
    hardware is pinned by its caller on the allocation it makes."""
    admission = await admit_org_machine(
        db,
        ctx=ctx,
        org_id=org_machine.org_team_id,
        owner_team_id=org_machine.owner_team_id,
        offering=offering,
        machine_type=machine_type,
        storage_gb=org_machine.storage_gb,
        org_machine=org_machine,
        now=now,
    )
    current = (
        await db.get(ComputeAllocation, org_machine.current_allocation_id)
        if org_machine.current_allocation_id is not None
        else None
    )
    if current is not None and current.state == ASLEEP:
        await pin_admitted_rates(db, current, admission)
    return admission


async def quota_left(
    db: AsyncSession, *, ctx: ActingContext, org_id: UUID, machine_type: ComputeMachineType
) -> int:
    """How many more machines of ``machine_type`` the org may buy now: the
    admitting grant's ceiling less the machines it holds, never below zero."""
    grant = await grants.resolve_machine_grant(
        db,
        grants.MachineGrantQuery(
            ctx=ctx, org_id=org_id, machine_type=machine_type, at=datetime.now(UTC)
        ),
    )
    if grant is None:
        return 0
    return max(grant.ceiling - await held_machines(db, org_id), 0)


async def pin_admitted_rates(
    db: AsyncSession, alloc: ComputeAllocation, admission: Admission
) -> None:
    """Pin what admission admitted on the allocation it was for: a grant's
    negotiated rate replaces the offering's that the allocation was created
    with, and the account is the one admission proved can pay."""
    alloc.price_per_minute_nanos = admission.rate_per_minute_nanos
    alloc.storage_price_per_minute_nanos = admission.storage_rate_per_minute_nanos
    if admission.billing_account_id is not None:
        await compute_funding().fund_allocation(db, alloc.id, admission.billing_account_id)


__all__ = [
    "Admission",
    "ComputeRefusedError",
    "ForeignOrgError",
    "MachineCreditError",
    "MachineQuotaReachedError",
    "NoPublicAddressError",
    "admit_org_machine",
    "admit_org_machine_start",
    "next_start_rates",
    "pin_admitted_rates",
    "purchase_rates",
    "quota_left",
    "runway_words",
    "start_rates",
]
