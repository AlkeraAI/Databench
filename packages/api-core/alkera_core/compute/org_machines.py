"""Machines an org holds: what state one is in, how it is drawn, and the
intents every path writes through.

* :func:`org_machine_state` is the one place the words people see come from:
  an org machine and the allocation backing it now, judged at ``now``.
* :func:`machine_card` draws an org machine for every surface, rates included
  only for a reader allowed to see them.
* :func:`create_org_machine`, :func:`new_allocation_for`,
  :func:`request_power` and :func:`request_delete` record what the org asked
  for. None of them calls a provider: the reconcile converges the provider
  machine on the row, so a request survives a crash between the two.
* :func:`funding_account_for` and :func:`in_audience` answer who pays and who
  may use it.
* :class:`ProviderTimings` (registered per provider kind and compute class)
  are what a starting step is expected to take, so a card can say "about 3
  min" instead of a spinner.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.boot_failure import node_error_of
from alkera_core.compute.disk import disk_nearly_full
from alkera_core.compute.machines import UNREACHABLE, machine_status
from alkera_core.compute.meter import compute_metering
from alkera_core.compute.pricing import compute_rate, storage_rate, true_storage_cost
from alkera_core.compute.transitions import transition
from alkera_core.compute.unservable import fault_read, is_unhealthy
from alkera_core.compute.wake import remember_wake
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import emit
from alkera_core.events.types import Entity, EventType
from alkera_core.models.compute import (
    ASLEEP,
    BOOTSTRAPPING,
    COMPUTE_TERMINAL_STATES,
    DEDICATED_TENANCY,
    DRAIN_CAP,
    DRAIN_CREDITS,
    DRAIN_USER,
    DRAINING,
    FAILED,
    FAILURE_CAPACITY,
    LOST,
    PENDING,
    PROVISIONED,
    PROVISIONING,
    READY,
    RELEASED,
    RELEASING,
    TERMINATED_BOOT_FAILED,
    TERMINATED_REPLACED,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute_offerings import AUDIENCE_LISTED, PRICING_FIXED, ComputeOffering
from alkera_core.models.org_machines import (
    GRANTED,
    GRANTEE_ORG,
    GRANTEE_TEAM,
    GRANTEE_USER,
    ORG_MACHINE_NAME_MAX,
    POWER_OFF,
    POWER_ON,
    STOP_NONE,
    STOP_REASONS,
    UNBILLED_ACQUISITIONS,
    USE_POOL,
    OrgComputeSettings,
    OrgMachine,
    OrgMachineAcquisition,
    OrgMachineAudience,
)
from alkera_core.models.team_membership import TeamMembership
from alkera_core.schemas.org_machines import (
    AudienceGrant,
    GpuSpec,
    MachineCard,
    MachineSpec,
    MachineWaitRead,
    OrgMachineState,
    StartingStep,
)
from alkera_core.status import MachineEvidence
from alkera_core.status import machine_status as machine_status_fact

#: The ``reason`` a transition records for an org machine's stop and start.
_STOP_REASON_PREFIX = "org machine stop"


# ---- provider timings ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderTimings:
    """How long each starting step usually takes on one provider, in seconds."""

    reserving: int
    booting: int
    installing: int


DEFAULT_TIMINGS = ProviderTimings(reserving=30, booting=90, installing=240)
_TIMINGS: dict[tuple[str, str], ProviderTimings] = {}


def register_timings(kind: str, compute_class: str, timings: ProviderTimings) -> None:
    """Register what starting a ``compute_class`` machine on provider ``kind``
    takes. A later registration for the same pair replaces the earlier one."""
    if min(timings.reserving, timings.booting, timings.installing) < 0:
        raise ValueError("a step cannot be expected to take negative time")
    _TIMINGS[(kind, compute_class)] = timings


def timings_for(kind: str, compute_class: str) -> ProviderTimings:
    """The registered timings for the pair, or :data:`DEFAULT_TIMINGS`. The
    built-in providers register theirs on import, so they are loaded first:
    the answer never depends on which provider module happened to be
    imported already."""
    from alkera_core.compute.provider import registered_kinds

    registered_kinds()
    return _TIMINGS.get((kind, compute_class), DEFAULT_TIMINGS)


def _expected_seconds(step: StartingStep, timings: ProviderTimings) -> int | None:
    if step == "reserving":
        return timings.reserving
    if step == "booting":
        return timings.booting
    if step == "installing":
        return timings.installing
    # Connecting is the box's first heartbeat: seconds, with nothing to wait on.
    return None


# ---- state ----------------------------------------------------------------------


def org_machine_state(
    om: OrgMachine, alloc: ComputeAllocation | None, *, now: datetime
) -> tuple[OrgMachineState, StartingStep | None]:
    """What the org machine is doing at ``now``, and the step when it is starting.

    ``alloc`` is the allocation backing it now (``current_allocation_id``);
    ``None`` when there is none. A machine wanted on with nothing backing it
    yet reads as reserving: the reconcile is about to ask for one.
    """
    if om.deleted_at is not None:
        return "deleted", None
    wanted_on = om.desired_power == POWER_ON
    if alloc is None:
        return ("starting", "reserving") if wanted_on else ("stopped", None)
    state = alloc.state
    if state == PENDING:
        return "starting", "reserving"
    if state == PROVISIONING:
        return "starting", ("booting" if alloc.provider_machine_id else "reserving")
    if state == BOOTSTRAPPING:
        return "starting", ("connecting" if alloc.registered_jti else "installing")
    if state == READY:
        if alloc.last_heartbeat_at is None:
            return "starting", "connecting"
        if machine_status(alloc, now=now) == UNREACHABLE:
            return "unreachable", None
        return ("unhealthy" if is_unhealthy(alloc) else "running"), None
    if state == DRAINING:
        if not wanted_on:
            return "stopping", None
        # A drain the org did not ask to stop (a move leaving it, the daemon
        # restarting in place): the machine is still up.
        return ("unreachable" if machine_status(alloc, now=now) == UNREACHABLE else "running"), None
    if state == RELEASING:
        return "stopping", None
    if state == ASLEEP:
        if wanted_on and alloc.capacity_gave_up_at is not None:
            # The provider had no hardware to start it on for the whole window.
            return "failed", None
        if wanted_on and alloc.wake_requested_at is not None:
            if alloc.failure_kind == FAILURE_CAPACITY:
                # The host it slept on has no free GPU; the reconcile keeps asking.
                return "waiting_for_hardware", None
            # A start was asked for and the reconcile is bringing it back.
            return "starting", "booting"
        return "stopped", None
    if state in (FAILED, LOST, RELEASED):
        if not wanted_on:
            return "stopped", None
        if (
            state == FAILED
            and alloc.failure_kind == FAILURE_CAPACITY
            and alloc.capacity_gave_up_at is None
        ):
            return "waiting_for_hardware", None
        return "failed", None
    raise ValueError(f"allocation {alloc.id} is in an unknown state {state!r}")


def _step_started_at(
    om: OrgMachine, alloc: ComputeAllocation | None, step: StartingStep
) -> datetime | None:
    if alloc is None:
        return om.updated_at
    if step == "connecting" and alloc.state == READY:
        return alloc.ready_at or alloc.state_changed_at
    if alloc.state == ASLEEP:
        return alloc.wake_requested_at
    return alloc.state_changed_at or alloc.created_at


def runs_free(om: OrgMachine | None, now: datetime) -> bool:
    """Whether ``om`` is charged nothing at ``now``: a machine the org runs
    itself (an unbilled acquisition), or a granted machine inside its free
    window. The one rule the card, a new allocation and admission's rates read."""
    if om is None:
        return False
    if om.acquisition in UNBILLED_ACQUISITIONS:
        return True
    return om.acquisition == GRANTED and om.free_until is not None and now < om.free_until


@dataclass(frozen=True, slots=True)
class AdmittedRate:
    """The compute rate and the paying account a start was admitted at.

    Admission resolves it for a purchase, a start or a replace (a negotiated
    grant's rate over the offering's); the reconcile carries it to the
    allocations it makes to finish that start. Every allocation is made with
    one (:func:`new_allocation_for`), so none is priced at a rate nobody
    resolved. Storage has no negotiated rate: :func:`pin_disk` prices it."""

    rate_per_minute_nanos: int
    billing_account_id: uuid.UUID | None


def storage_price(om: OrgMachine, offering: ComputeOffering, *, now: datetime) -> int:
    """What ``om``'s disk costs a minute now: nothing while a granted machine
    is free, else the offering's storage rate for its size."""
    return 0 if runs_free(om, now) else storage_rate(offering, om.storage_gb)


def pin_disk(
    alloc: ComputeAllocation, om: OrgMachine, offering: ComputeOffering, *, now: datetime
) -> None:
    """Give ``alloc`` the disk ``om`` asks for, at its price now
    (:func:`storage_price`). A new allocation and a grown disk are priced the
    same way."""
    alloc.storage_gb = om.storage_gb
    alloc.storage_price_per_minute_nanos = storage_price(om, offering, now=now)
    alloc.true_storage_cost_per_minute_nanos = true_storage_cost(offering, om.storage_gb)


def disk_to_grow(om: OrgMachine, alloc: ComputeAllocation) -> bool:
    """Whether ``om`` asks for a bigger disk than its provider machine has. Only
    a machine the plane provisioned for it, at a size the plane knows, is ever
    grown: a box attached to the org from elsewhere keeps the disk it came
    with."""
    return (
        om.deleted_at is None
        and alloc.origin == PROVISIONED
        and 0 < alloc.storage_gb < om.storage_gb
    )


#: The drains money forces: the credit ran out, or the machine reached its cap.
MONEY_DRAIN_KINDS: frozenset[str] = frozenset({DRAIN_CREDITS, DRAIN_CAP})


def money_drain_stops_at(alloc: ComputeAllocation | None) -> datetime | None:
    """When an allocation draining because money ran out stops: the deadline
    its drain was given. ``None`` for any other allocation."""
    if alloc is None or alloc.state != DRAINING or alloc.drain_kind not in MONEY_DRAIN_KINDS:
        return None
    return alloc.drain_deadline_at


async def drain_stops_at(db: AsyncSession, om: OrgMachine) -> datetime | None:
    """When a machine draining for money stops: the deadline its drain was
    given, which is the end of the window its credit hold covers. ``None``
    unless the machine is in a credit or cap drain right now."""
    if om.current_allocation_id is None:
        return None
    return money_drain_stops_at(await db.get(ComputeAllocation, om.current_allocation_id))


def _failure(state: str, alloc: ComputeAllocation | None) -> tuple[str, str]:
    """Why a ``failed`` machine stopped trying, and the last error on record:
    ``never_registered`` when its node never claimed it (the node's own error),
    ``start_failed`` when its provider refused or failed the start (the
    provider's words, as the launch recorded them), else nothing."""
    if state != "failed" or alloc is None or alloc.state != FAILED:
        return "", ""
    if alloc.terminated_reason == TERMINATED_BOOT_FAILED:
        return "never_registered", node_error_of(alloc.error)
    if alloc.failure_kind != FAILURE_CAPACITY and (alloc.error or "").strip():
        return "start_failed", " ".join((alloc.error or "").split())
    return "", ""


def machine_card(
    om: OrgMachine,
    alloc: ComputeAllocation | None,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    *,
    now: datetime,
    include_rate: bool,
    next_rates: tuple[int, int] | None = None,
    wait: MachineWaitRead | None = None,
) -> MachineCard:
    """The org machine as every surface draws it. Rates appear only when
    ``include_rate``: the rates a running session pinned while one runs, else
    what a start or a wake would pin now (a stopped machine shows today's
    rate, which its next wake pins). ``next_rates`` is that rate as admission
    reads it (a negotiated grant included); without it, the offering's."""
    state, step = org_machine_state(om, alloc, now=now)
    rate: int | None = None
    storage: int | None = None
    if include_rate:
        if (
            alloc is not None
            and alloc.state not in COMPUTE_TERMINAL_STATES
            and alloc.state != ASLEEP
        ):
            rate = alloc.price_per_minute_nanos
            storage = alloc.storage_price_per_minute_nanos
        elif next_rates is not None:
            rate, storage = next_rates
        elif runs_free(om, now):
            rate, storage = 0, 0
        else:
            rate = compute_rate(offering, machine_type)
            storage = storage_rate(offering, om.storage_gb)
    gpu = (
        GpuSpec(
            name=machine_type.gpu_name,
            count=machine_type.gpu_count,
            memory_gb=machine_type.gpu_memory_gb,
        )
        if machine_type.gpu_count > 0
        else None
    )
    timings = timings_for(machine_type.provider, machine_type.compute_class)
    step_started_at = _step_started_at(om, alloc, step) if step is not None else None
    disk_full = state == "running" and alloc is not None and disk_nearly_full(alloc.resources_json)
    step_expected_seconds = _expected_seconds(step, timings) if step is not None else None
    shown_wait = wait if state == "waiting_for_hardware" else None
    failure, failure_detail = _failure(state, alloc)
    return MachineCard(
        kind="org_machine",
        org_machine_id=str(om.id),
        name=om.name,
        spec=MachineSpec(
            offering_name=offering.name,
            provider=machine_type.provider,
            region=offering.region,
            gpu=gpu,
            vcpu=machine_type.vcpu,
            memory_gb=machine_type.memory_gb,
            disk_gb=om.storage_gb,
            rate_per_minute_nanos=rate,
            storage_rate_per_minute_nanos=storage,
        ),
        state=state,
        step=step,
        step_started_at=step_started_at,
        step_expected_seconds=step_expected_seconds,
        stop_reason=om.stop_reason,
        drain_stops_at=money_drain_stops_at(alloc),
        fault=fault_read(alloc, staff=False) if state == "unhealthy" and alloc else None,
        disk_full=disk_full,
        status=machine_status_fact(
            MachineEvidence(
                state=state,
                name=om.name,
                step=step,
                step_started_at=step_started_at,
                step_expected_seconds=step_expected_seconds,
                stop_reason=om.stop_reason,
                gpu_name=gpu.name if gpu is not None else "",
                disk_full=disk_full,
                wait_reason=shown_wait.reason if shown_wait else "",
                wait_retry_at=shown_wait.next_try_at if shown_wait else None,
                failure=failure,
                failure_detail=failure_detail,
            ),
            now=now,
        ),
        wait=shown_wait,
    )


# ---- intents ----------------------------------------------------------------------


def _uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


#: The largest volume an unlisted machine's offering admits, in GB.
UNLISTED_STORAGE_MAX_GB = 1_000_000


async def unlisted_offering_for(
    db: AsyncSession, machine_type: ComputeMachineType
) -> ComputeOffering:
    """The offering a machine the org did not buy is recorded under (a host it
    added, a box that registered itself): the type's oldest offering, or an
    unpurchasable one at a rate of zero made on first use. It appears in no
    buy list. Flushes, never commits."""
    offering = (
        await db.execute(
            select(ComputeOffering)
            .where(ComputeOffering.machine_type_id == machine_type.id)
            .order_by(ComputeOffering.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if offering is not None:
        return offering
    offering = ComputeOffering(
        machine_type_id=machine_type.id,
        name=machine_type.display_name,
        pricing_mode=PRICING_FIXED,
        fixed_rate_per_minute_nanos=0,
        storage_gb_default=1,
        storage_gb_max=UNLISTED_STORAGE_MAX_GB,
        storage_rate_per_gb_month_nanos=0,
        audience=AUDIENCE_LISTED,
        purchasable=False,
    )
    db.add(offering)
    await db.flush()
    return offering


async def create_org_machine(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    owner_team_id: uuid.UUID,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    name: str,
    acquisition: OrgMachineAcquisition,
    free_until: datetime | None,
    use_mode: Literal["pool", "assigned"],
    storage_gb: int,
    audience: Sequence[AudienceGrant],
    idle_stop_minutes: int | None,
    monthly_cap_nanos: int | None,
    admitted: AdmittedRate,
    created_by: uuid.UUID | None,
    now: datetime | None = None,
) -> OrgMachine:
    """Insert the org machine, its audience and a ``pending`` allocation for it.

    The org is the caller's, never the client's; the database refuses an
    audience team or an allocation of another org. No provider is called: the
    reconcile provisions the pending allocation. Flushes, never commits.
    """
    if created_by is None:
        raise ValueError("an org machine's first allocation needs the person it was created by")
    if offering.machine_type_id != machine_type.id:
        raise ValueError("the machine type is not the offering's")
    om = OrgMachine(
        org_team_id=org_id,
        owner_team_id=owner_team_id,
        offering_id=offering.id,
        name=name,
        acquisition=acquisition,
        free_until=free_until,
        use_mode=use_mode,
        storage_gb=storage_gb,
        idle_stop_minutes=idle_stop_minutes,
        monthly_cap_nanos=monthly_cap_nanos,
        desired_power=POWER_ON,
        stop_reason=STOP_NONE,
        created_by=created_by,
    )
    db.add(om)
    await db.flush()
    db.add_all(
        [
            OrgMachineAudience(
                org_team_id=org_id,
                org_machine_id=om.id,
                grantee_kind=grant.kind,
                team_id=_uuid(grant.team_id),
                user_id=_uuid(grant.user_id),
                created_by=created_by,
            )
            for grant in _distinct(audience)
        ]
    )
    await new_allocation_for(
        db,
        om,
        offering=offering,
        machine_type=machine_type,
        user_id=created_by,
        admitted=admitted,
        now=now,
    )
    return om


def _distinct(audience: Iterable[AudienceGrant]) -> list[AudienceGrant]:
    seen: set[tuple[str, str | None, str | None]] = set()
    out: list[AudienceGrant] = []
    for grant in audience:
        key = (grant.kind, grant.team_id, grant.user_id)
        if key not in seen:
            seen.add(key)
            out.append(grant)
    return out


async def new_allocation_for(
    db: AsyncSession,
    om: OrgMachine,
    *,
    offering: ComputeOffering,
    machine_type: ComputeMachineType,
    user_id: uuid.UUID,
    admitted: AdmittedRate,
    now: datetime | None = None,
) -> ComputeAllocation:
    """A ``pending`` allocation to back ``om`` at the rate its start was
    admitted at (nothing while a granted machine is free), its disk priced,
    made the org machine's current one. Flushes, never commits."""
    moment = now or datetime.now(UTC)
    free = runs_free(om, moment)
    alloc = ComputeAllocation(
        user_id=user_id,
        org_team_id=om.org_team_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        origin=PROVISIONED,
        tenancy=DEDICATED_TENANCY,
        tenant_org_id=om.org_team_id,
        org_machine_id=om.id,
        name=om.name,
        state=PENDING,
        price_per_minute_nanos=0 if free else admitted.rate_per_minute_nanos,
        true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
    )
    pin_disk(alloc, om, offering, now=moment)
    db.add(alloc)
    await db.flush()
    # A machine the org runs itself debits no account, whoever asked.
    funded_by = None if om.acquisition in UNBILLED_ACQUISITIONS else admitted.billing_account_id
    await compute_funding().fund_allocation(db, alloc.id, funded_by)
    previous = await _current_allocation(db, om)
    if previous is not None:
        retire_allocation(db, previous, now=moment)
    om.current_allocation_id = alloc.id
    await db.flush()
    return alloc


def retire_allocation(
    db: AsyncSession, alloc: ComputeAllocation, *, now: datetime, reason: str = TERMINATED_REPLACED
) -> None:
    """End an allocation that stops being its org machine's current one, in
    the caller's transaction. One the provider never made a machine for fails;
    anything else goes to ``releasing``, for the node reconcile to terminate
    its machine and free its volume. Without this a replaced machine stayed
    asleep with nothing to end it. Already ending: left alone."""
    if alloc.state in COMPUTE_TERMINAL_STATES or alloc.state == RELEASING:
        return
    alloc.terminated_reason = alloc.terminated_reason or reason
    alloc.wake_requested_at = None
    alloc.wake_request_json = None
    target = FAILED if alloc.state == PENDING else RELEASING
    transition(db, alloc, target, reason=f"no longer current: {reason}", now=now)


async def _current_allocation(db: AsyncSession, om: OrgMachine) -> ComputeAllocation | None:
    if om.current_allocation_id is None:
        return None
    return (
        await lock_rows(
            db,
            LockRank.ALLOCATION,
            select(ComputeAllocation).where(
                ComputeAllocation.id == om.current_allocation_id,
                ComputeAllocation.tenant_org_id == om.org_team_id,
            ),
        )
    ).scalar_one_or_none()


async def announce(
    db: AsyncSession,
    om: OrgMachine,
    alloc: ComputeAllocation | None,
    *,
    actor: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> None:
    """Ring ``org_machine.changed`` for ``om``: its state, step and stop
    reason, and never a price."""
    state, step = org_machine_state(om, alloc, now=now or datetime.now(UTC))
    await emit(
        db,
        org_id=om.org_team_id,
        type=EventType.ORG_MACHINE_CHANGED,
        entity=Entity.ORG_MACHINE,
        entity_id=str(om.id),
        version=om.version,
        payload={"state": state, "step": step, "stop_reason": om.stop_reason},
        actor=actor,
    )


async def request_power(
    db: AsyncSession,
    om: OrgMachine,
    *,
    desired: Literal["on", "off"],
    reason: str,
    drain_kind: str | None,
    deadline: datetime | None,
    actor: Mapping[str, Any],
    now: datetime | None = None,
) -> None:
    """Record that the org wants ``om`` on or off, and start the allocation
    toward it where that needs no provider call.

    Off: a ``ready`` allocation starts draining (``drain_kind``, the deadline
    or now); one still coming up is left for the reconcile to stop once it
    lands. On: an ``asleep`` allocation gets a wake the reconcile completes;
    a terminal or missing one is replaced by the reconcile. ``actor`` is an
    actor-chain document. Flushes, never commits.
    """
    if om.deleted_at is not None:
        raise ValueError(f"org machine {om.id} is deleted")
    moment = now or datetime.now(UTC)
    alloc = await _current_allocation(db, om)
    if desired == POWER_OFF:
        if reason not in STOP_REASONS or reason == STOP_NONE:
            raise ValueError(f"{reason!r} is not a stop reason")
        om.desired_power = POWER_OFF
        om.stop_reason = reason
        if alloc is not None and alloc.state == READY:
            transition(
                db,
                alloc,
                DRAINING,
                reason=f"{_STOP_REASON_PREFIX}: {reason}",
                actor=dict(actor),
                now=moment,
            )
            alloc.drain_kind = drain_kind or DRAIN_USER
            alloc.drain_deadline_at = deadline or moment
    elif desired == POWER_ON:
        om.desired_power = POWER_ON
        om.stop_reason = STOP_NONE
        if alloc is not None and alloc.state == ASLEEP:
            if alloc.capacity_gave_up_at is not None:
                # Asked again after the reconcile stopped waiting for hardware:
                # a fresh window, and nothing known yet about this try.
                alloc.capacity_gave_up_at = None
                alloc.failure_kind = ""
            remember_wake(
                alloc,
                actor=dict(actor),
                chain=dict(actor),
                reason=reason or "start",
                now=moment,
            )
    else:
        raise ValueError(f"{desired!r} is not a power state")
    om.version += 1
    om.updated_at = moment
    await db.flush()
    await announce(db, om, alloc, actor=actor, now=moment)


async def request_delete(
    db: AsyncSession, om: OrgMachine, *, actor: Mapping[str, Any], now: datetime | None = None
) -> None:
    """Delete ``om``: it reads deleted, is wanted off, has no audience, and no
    workspace stays pinned to it or takes it as the org's default. A workspace
    that was pinned remembers the machine it lost, so its next wake asks where
    to run (``WorkspaceSpec.lost_machine_id``). The reconcile releases its
    allocation. Flushes, never commits."""
    if om.deleted_at is not None:
        return
    moment = now or datetime.now(UTC)
    om.deleted_at = moment
    om.desired_power = POWER_OFF
    om.version += 1
    om.updated_at = moment
    await db.execute(
        delete(OrgMachineAudience).where(
            OrgMachineAudience.org_machine_id == om.id,
            OrgMachineAudience.org_team_id == om.org_team_id,
        )
    )
    unpinned = (
        await db.execute(
            text(
                "UPDATE workspace_objects "
                "SET spec = spec || jsonb_build_object("
                "      'machine_pin', NULL, 'lost_machine_id', :pin, 'fell_back_at', NULL), "
                "    version = version + 1, updated_at = now() "
                "WHERE org_team_id = :org AND type = 'workspace' "
                "  AND spec->>'machine_pin' = :pin "
                "RETURNING id, version"
            ),
            {"org": om.org_team_id, "pin": str(om.id)},
        )
    ).all()
    # New workspaces stop landing on it too.
    await db.execute(
        update(OrgComputeSettings)
        .where(
            OrgComputeSettings.org_team_id == om.org_team_id,
            OrgComputeSettings.default_org_machine_id == om.id,
        )
        .values(
            default_org_machine_id=None,
            version=OrgComputeSettings.version + 1,
            updated_at=moment,
        )
    )
    await db.flush()
    for object_id, version in unpinned:
        await emit(
            db,
            org_id=om.org_team_id,
            type=EventType.WORKSPACE_OBJECT_CHANGED,
            entity=Entity.WORKSPACE_OBJECT,
            entity_id=str(object_id),
            version=int(version),
            payload={"type": "workspace", "version": int(version)},
            actor=actor,
        )
    await announce(db, om, None, actor=actor, now=moment)


# ---- money and use -------------------------------------------------------------------


async def funding_account_for(db: AsyncSession, om: OrgMachine) -> uuid.UUID | None:
    """The account the machine's minutes debit, as the compute biller names it
    (the owner team's pool when it has one, else the org's pool). ``None`` when
    neither exists, or when nothing bills compute."""
    return await compute_metering().machine_funding_account(
        db, org_id=om.org_team_id, owner_team_id=om.owner_team_id
    )


def in_audience(
    om: OrgMachine,
    audience_rows: Iterable[OrgMachineAudience],
    *,
    user_id: uuid.UUID,
    user_team_ids: set[uuid.UUID],
) -> bool:
    """Whether a member of the machine's org may use it. A pool machine serves
    every member; an assigned one its audience (a team grant covers its
    sub-teams because membership is materialized on every ancestor, so
    ``user_team_ids`` already holds them). A deleted machine serves nobody.
    Rows of another machine or another org are ignored."""
    if om.deleted_at is not None:
        return False
    if om.use_mode == USE_POOL:
        return True
    for row in audience_rows:
        if row.org_machine_id != om.id or row.org_team_id != om.org_team_id:
            continue
        if row.grantee_kind == GRANTEE_ORG:
            return True
        if row.grantee_kind == GRANTEE_TEAM and row.team_id in user_team_ids:
            return True
        if row.grantee_kind == GRANTEE_USER and row.user_id == user_id:
            return True
    return False


async def team_ids_of(db: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID) -> set[uuid.UUID]:
    """Every team of the org the person sits on: membership rows are written
    on the team and each team above it, so one read answers "in this team or
    a team below it" for every team at once."""
    rows = await db.execute(
        select(TeamMembership.team_id).where(
            TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_id
        )
    )
    return set(rows.scalars().all())


async def audience_of(db: AsyncSession, machine: OrgMachine) -> list[OrgMachineAudience]:
    rows = await db.execute(
        select(OrgMachineAudience).where(
            OrgMachineAudience.org_machine_id == machine.id,
            OrgMachineAudience.org_team_id == machine.org_team_id,
        )
    )
    return list(rows.scalars().all())


async def may_use(db: AsyncSession, machine: OrgMachine, *, user_id: uuid.UUID) -> bool:
    """Whether the person is in the machine's audience (every member of the org
    for a pool machine). Two reads: their teams and the machine's grants."""
    teams = await team_ids_of(db, user_id=user_id, org_id=machine.org_team_id)
    return in_audience(
        machine, await audience_of(db, machine), user_id=user_id, user_team_ids=teams
    )


async def new_workspace_pin(
    db: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID | None:
    """The org machine a new workspace of ``user_id``'s is pinned to when they
    name none: the org's default for new workspaces, when it is a live machine
    of the org they may use (its audience, or every member for a pool
    machine, the use an explicit "Run on" is decided by). ``None`` otherwise,
    and the workspace takes the default placement."""
    row = await db.get(OrgComputeSettings, org_id)
    if row is None or row.default_org_machine_id is None:
        return None
    machine = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id == row.default_org_machine_id,
                OrgMachine.org_team_id == org_id,
                OrgMachine.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if machine is None or not await may_use(db, machine, user_id=user_id):
        return None
    return machine.id


async def free_machine_name(db: AsyncSession, *, org_id: uuid.UUID, wanted: str) -> str:
    """``wanted``, or the first of ``wanted 2``, ``wanted 3``... that no live
    machine of the org holds.

    Names are unique per org among live machines, compared without case, so a
    name the server picks for a machine nobody named must step around the ones
    already there instead of failing the insert. The base is cut so the suffix
    always fits the column. A name taken between this read and the insert is
    still refused by the unique index, as a conflict.
    """
    taken = set(
        (
            await db.execute(
                select(func.lower(OrgMachine.name)).where(
                    OrgMachine.org_team_id == org_id, OrgMachine.deleted_at.is_(None)
                )
            )
        ).scalars()
    )
    base = wanted[:ORG_MACHINE_NAME_MAX]
    if base.lower() not in taken:
        return base
    n = 2
    while True:
        suffix = f" {n}"
        candidate = base[: ORG_MACHINE_NAME_MAX - len(suffix)].rstrip() + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


__all__ = [
    "DEFAULT_TIMINGS",
    "UNLISTED_STORAGE_MAX_GB",
    "AdmittedRate",
    "OrgMachineState",
    "ProviderTimings",
    "StartingStep",
    "announce",
    "audience_of",
    "create_org_machine",
    "disk_to_grow",
    "drain_stops_at",
    "free_machine_name",
    "funding_account_for",
    "in_audience",
    "machine_card",
    "may_use",
    "money_drain_stops_at",
    "new_allocation_for",
    "new_workspace_pin",
    "org_machine_state",
    "pin_disk",
    "register_timings",
    "request_delete",
    "request_power",
    "retire_allocation",
    "runs_free",
    "storage_price",
    "team_ids_of",
    "timings_for",
    "unlisted_offering_for",
]
