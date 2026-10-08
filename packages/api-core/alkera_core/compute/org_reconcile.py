"""Make every org machine's provider machine match what its org asked for.

People express intent on ``org_machines`` (``desired_power``, ``deleted_at``,
through :func:`~alkera_core.compute.org_machines.request_power` and
``request_delete``); nothing they do calls a provider. This pass, every 30
seconds on the money queue, converges each org machine's current allocation
to that intent, one step per machine per pass:

=============================================  =========================================
org machine / its allocation                   step
=============================================  =========================================
on; none, or released / lost                   a fresh allocation (replace)
on; failed for capacity (or a throttle)        a fresh allocation every
                                               ``machine_capacity_retry_every_seconds``
                                               until ``machine_capacity_retry_minutes``
                                               after the first such failure, then left
                                               failed
on; failed to boot                             a fresh allocation, up to
                                               ``machine_boot_retry_count`` times
on; failed otherwise (invalid, auth, quota)    left failed: asking again changes nothing
on; pending                                    launched (:mod:`alkera_core.compute.launch`)
on; asleep with a start asked for              started; a capacity refusal is kept on the
                                               row (``failure_kind``) and tried each pass
                                               within the capacity window, then dropped
                                               and marked ``capacity_gave_up_at``
on; draining for a stop                        back to ready (the stop was taken back)
on; ready and idle past its window             asked to stop (``drain_kind='idle'``)
on; ready, silent or faulted past the bound    stopped (``stop_reason="not_responding"``):
                                               ``machine_unservable_stop_minutes``
a bigger disk asked for, grown online          grown in place, ready or asleep
a bigger disk asked for, grown on a restart    ready: drained (the move grace), then
                                               slept; asleep: grown, then started if on
off; ready                                     drained toward the stop
off; draining, empty or past its deadline      slept (:mod:`alkera_core.compute.power`)
deleted; ready                                 drained, then released
deleted; draining, empty or past its deadline  released (terminated, disk destroyed,
                                               credential and node token revoked)
deleted; pending / starting / asleep           released
=============================================  =========================================

``provisioning`` and ``bootstrapping`` rows are the node reconcile's: it moves
them on the provider's word and ends a boot that took too long as
``boot_failed``. A failed or released allocation is never revived: replacing it
is a new allocation of the same offering, so the org machine keeps its id,
name, audience and pins, and a disk the provider lost is never pretended back.

Every step is decided by the pure :func:`decide` from the rows and the clock,
then acted on under the org machine's row lock; a change of what people see
(:func:`~alkera_core.compute.org_machines.org_machine_state`) is announced as
``org_machine.changed``. The pass reads across orgs through
:func:`~alkera_core.db.cross_tenant.cross_tenant_read` and writes each machine
through :func:`~alkera_core.db.cross_tenant.cross_tenant_write`, like every
platform pass. It never writes an ``org_compute_assignments`` row.
"""

from __future__ import annotations

import asyncio
import enum
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.disk import GrowRule, disk_rules_for, grow_rule
from alkera_core.compute.events import announce_machine
from alkera_core.compute.handoff import leave_placement
from alkera_core.compute.idle import PoolMachine, is_idle, pool_machines_to_stop
from alkera_core.compute.launch import launch_allocation
from alkera_core.compute.machines import DRAINING as DRAINING_STATUS
from alkera_core.compute.node_reconcile import revoke_node_credentials, settle_release
from alkera_core.compute.nodes import NodeProvider
from alkera_core.compute.org_machines import (
    AdmittedRate,
    announce,
    disk_to_grow,
    funding_account_for,
    new_allocation_for,
    org_machine_state,
    pin_disk,
    request_power,
    storage_price,
)
from alkera_core.compute.power import sleep_org_allocation
from alkera_core.compute.power_lock import stop_after_sleep
from alkera_core.compute.provider import (
    CAPACITY_FAILURE,
    TRANSIENT_FAILURE,
    ComputeProviderError,
    provider_traits,
)
from alkera_core.compute.start_runway import runway_shortfall, start_runway_nanos
from alkera_core.compute.transitions import transition
from alkera_core.compute.unservable import unservable_since
from alkera_core.compute.wake import drop_wake, remember_wake, wake_allocation
from alkera_core.config import Settings
from alkera_core.db.cross_tenant import cross_tenant_read, cross_tenant_write
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import actor_system
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    ASLEEP,
    COMPUTE_TERMINAL_STATES,
    DRAIN_CAP,
    DRAIN_CREDITS,
    DRAIN_IDLE,
    DRAIN_MOVE,
    DRAIN_USER,
    DRAINING,
    FAILED,
    LOST,
    PENDING,
    READY,
    RELEASED,
    RELEASING,
    TERMINATED_BOOT_FAILED,
    TERMINATED_REPLACED,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import (
    GRANTED,
    POWER_OFF,
    POWER_ON,
    STOP_CAP,
    STOP_CREDITS,
    STOP_FREE_EXPIRED,
    STOP_IDLE,
    STOP_MOVED,
    STOP_NOT_RESPONDING,
    USE_POOL,
    OrgComputeSettings,
    OrgMachine,
    OrgMachineAudience,
)

log = get_logger(__name__)

RECONCILE_ACTOR: dict[str, Any] = {"kind": "system", "email": None, "name": "reconcile"}

#: How long a deleted machine's drain waits for the turns its box still holds
#: before it is released regardless.
DELETE_DRAIN = timedelta(minutes=5)

#: The drains this pass may take back when the org wants the machine on again:
#: the stops an org machine's own power asked for. A drain of the daemon's own
#: (a restart, a stop) or a workspace move is somebody else's to finish.
STOP_DRAIN_KINDS: frozenset[str | None] = frozenset(
    {DRAIN_IDLE, DRAIN_USER, DRAIN_CREDITS, DRAIN_CAP}
)

#: The failure kinds a fresh allocation is tried again for, within the capacity
#: window: no hardware now, or a provider that kept failing to answer.
RETRIED_FAILURE_KINDS: frozenset[str] = frozenset({CAPACITY_FAILURE, TRANSIENT_FAILURE})

#: ``stop_reason`` onto the drain a stop runs as.
_DRAIN_FOR_STOP: dict[str, str] = {
    STOP_IDLE: DRAIN_IDLE,
    STOP_CREDITS: DRAIN_CREDITS,
    STOP_CAP: DRAIN_CAP,
    STOP_MOVED: DRAIN_MOVE,
}

Sleep = Callable[[float], Awaitable[None]]
ProviderLookup = Callable[[str], NodeProvider]


class Step(enum.StrEnum):
    """What one pass does for one org machine."""

    NOTHING = "nothing"
    NEW_ALLOCATION = "new_allocation"
    LAUNCH = "launch"
    START = "start"
    GIVE_UP_START = "give_up_start"
    EXHAUSTED = "exhausted"
    UNDRAIN = "undrain"
    DRAIN = "drain"
    SLEEP = "sleep"
    STOP_UNSERVABLE = "stop_unservable"
    DRAIN_FOR_DISK = "drain_for_disk"
    GROW = "grow"
    RELEASE = "release"


@dataclass(frozen=True)
class RetryWindow:
    """The settings the decision reads: how a failed start is tried again."""

    capacity_retry: timedelta
    capacity_retry_every: timedelta
    boot_retries: int
    #: How long a running machine may stay unable to serve before it stops.
    unservable_stop: timedelta = timedelta(minutes=30)

    @classmethod
    def from_settings(cls, config: Settings) -> RetryWindow:
        return cls(
            capacity_retry=timedelta(minutes=config.machine_capacity_retry_minutes),
            capacity_retry_every=timedelta(seconds=config.machine_capacity_retry_every_seconds),
            boot_retries=config.machine_boot_retry_count,
            unservable_stop=timedelta(minutes=config.machine_unservable_stop_minutes),
        )


def _ended_at(alloc: ComputeAllocation) -> datetime:
    return alloc.released_at or alloc.state_changed_at or alloc.created_at


def _failure_kind(alloc: ComputeAllocation) -> str:
    return alloc.failure_kind or ""


def _streak(
    history: Sequence[ComputeAllocation], matches: Callable[[ComputeAllocation], bool]
) -> list[ComputeAllocation]:
    """The newest allocations of the org machine, while each ``matches``."""
    run: list[ComputeAllocation] = []
    for alloc in history:
        if not matches(alloc):
            break
        run.append(alloc)
    return run


@dataclass(frozen=True, slots=True)
class CapacityWait:
    """Where a machine waiting for hardware stands in its retry window."""

    #: When the reconcile asks the provider again (``None``: every pass).
    next_try_at: datetime | None
    #: When it stops asking and the machine reads "couldn't start".
    gives_up_at: datetime


def capacity_wait(
    alloc: ComputeAllocation | None,
    history: Sequence[ComputeAllocation],
    *,
    window: RetryWindow,
) -> CapacityWait | None:
    """The retry schedule of a machine the provider has no hardware for, or
    ``None`` when it is not waiting. The one schedule the reconcile acts on
    and the machine card says: a failed create is tried again on a fresh
    allocation every ``capacity_retry_every`` until ``capacity_retry`` after
    the first failure of this try; a start of a stopped one is asked every
    pass until ``capacity_retry`` after it was asked for."""
    if alloc is None:
        return None
    if alloc.state == ASLEEP and alloc.wake_requested_at is not None:
        if alloc.capacity_gave_up_at is not None or _failure_kind(alloc) != CAPACITY_FAILURE:
            return None
        return CapacityWait(
            next_try_at=None, gives_up_at=alloc.wake_requested_at + window.capacity_retry
        )
    if alloc.state != FAILED or _failure_kind(alloc) not in RETRIED_FAILURE_KINDS:
        return None
    if alloc.capacity_gave_up_at is not None:
        return None
    # The failures of this try: the newest allocations that failed the same
    # way, back to one the reconcile already gave up on (a "try again" after
    # that starts a window of its own).
    failures = _streak(
        history,
        lambda a: (
            a.state == FAILED
            and _failure_kind(a) in RETRIED_FAILURE_KINDS
            and a.capacity_gave_up_at is None
        ),
    )
    first = min(_ended_at(a) for a in failures) if failures else _ended_at(alloc)
    return CapacityWait(
        next_try_at=_ended_at(alloc) + window.capacity_retry_every,
        gives_up_at=first + window.capacity_retry,
    )


def _retry_after_failure(
    alloc: ComputeAllocation,
    history: Sequence[ComputeAllocation],
    *,
    now: datetime,
    window: RetryWindow,
) -> Step:
    """Whether a failed allocation is replaced by a fresh one now."""
    if alloc.terminated_reason == TERMINATED_BOOT_FAILED:
        failures = _streak(history, lambda a: a.terminated_reason == TERMINATED_BOOT_FAILED)
        return Step.NEW_ALLOCATION if len(failures) <= window.boot_retries else Step.NOTHING
    wait = capacity_wait(alloc, history, window=window)
    if wait is None:
        return Step.NOTHING
    if now >= wait.gives_up_at:
        # Out of window: the org machine stops reading "waiting for hardware"
        # and reads "couldn't start" until someone tries again.
        return Step.EXHAUSTED
    if wait.next_try_at is not None and now >= wait.next_try_at:
        return Step.NEW_ALLOCATION
    return Step.NOTHING


def _drain_over(alloc: ComputeAllocation, *, now: datetime) -> bool:
    deadline = alloc.drain_deadline_at
    return alloc.chats_served == 0 or deadline is None or deadline <= now


async def _grow_rule(db: AsyncSession, om: OrgMachine, alloc: ComputeAllocation | None) -> GrowRule:
    """How ``alloc``'s volume may grow, read only when ``om`` asks for more."""
    if alloc is None or not disk_to_grow(om, alloc) or alloc.machine_type_id is None:
        return GrowRule.NEVER
    machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
    if machine_type is None:
        return GrowRule.NEVER
    rules = disk_rules_for(machine_type.provider, machine_type.compute_class)
    return grow_rule(rules, alloc.capabilities_json)


def decide(
    om: OrgMachine,
    alloc: ComputeAllocation | None,
    history: Sequence[ComputeAllocation],
    *,
    now: datetime,
    window: RetryWindow,
    grow: GrowRule,
) -> Step:
    """The one step this pass takes for ``om`` (the module's table).

    ``alloc`` is the current allocation; ``history`` every allocation the org
    machine has had, newest first (the current one included); ``grow`` how
    its volume may grow (:func:`~alkera_core.compute.disk.grow_rule`)."""
    if om.deleted_at is not None:
        if alloc is None or alloc.state in COMPUTE_TERMINAL_STATES or alloc.state == RELEASING:
            return Step.NOTHING
        if alloc.state == READY:
            return Step.DRAIN
        if alloc.state == DRAINING:
            return Step.RELEASE if _drain_over(alloc, now=now) else Step.NOTHING
        return Step.RELEASE
    if alloc is None:
        return Step.NEW_ALLOCATION if om.desired_power == POWER_ON else Step.NOTHING
    state = alloc.state
    if grow == GrowRule.ONLINE and disk_to_grow(om, alloc) and state in (READY, ASLEEP):
        return Step.GROW
    if grow == GrowRule.RESTART and disk_to_grow(om, alloc):
        # The provider grows a volume only while the machine is stopped: drain
        # it the way a stop does, sleep it, grow it. Whether it starts again
        # is the org's wanting it on, as after any stop.
        if state == READY:
            return Step.DRAIN_FOR_DISK
        if state == DRAINING:
            return Step.SLEEP if _drain_over(alloc, now=now) else Step.NOTHING
        if state == ASLEEP:
            return Step.GROW
    if (
        om.desired_power == POWER_ON
        and state == READY
        and (since := unservable_since(alloc, now=now)) is not None
        and now - since >= window.unservable_stop
    ):
        # Silent, or unable to run chats, for the whole bound: its minutes
        # are not billed but the provider bills us, and a box that cannot
        # reach the API is fixed only by new hardware.
        return Step.STOP_UNSERVABLE
    if om.desired_power == POWER_OFF:
        if state == READY:
            return Step.DRAIN
        if state == DRAINING and alloc.drain_kind != DRAIN_MOVE:
            return Step.SLEEP if _drain_over(alloc, now=now) else Step.NOTHING
        return Step.NOTHING
    # The org wants it on.
    if state in (RELEASED, LOST):
        return Step.NEW_ALLOCATION
    if state == FAILED:
        return _retry_after_failure(alloc, history, now=now, window=window)
    if state == PENDING:
        return Step.LAUNCH
    if state == ASLEEP:
        if alloc.wake_requested_at is None:
            # Asleep and wanted on, but nobody asked for it: it starts when a
            # message or a manager asks, never on its own.
            return Step.NOTHING
        if now - alloc.wake_requested_at >= window.capacity_retry:
            return Step.GIVE_UP_START
        return Step.START
    if state == DRAINING and alloc.drain_kind in STOP_DRAIN_KINDS:
        return Step.UNDRAIN
    return Step.NOTHING


@dataclass
class OrgReconcileSummary:
    checked: int = 0
    steps: dict[str, int] = field(default_factory=dict)
    announced: int = 0
    provider_errors: int = 0
    idle_stops: int = 0
    #: Machines whose step ran past :data:`STEP_BUDGET` and was given up on.
    overran: int = 0

    def count(self, step: Step) -> None:
        if step is not Step.NOTHING:
            self.steps[step.value] = self.steps.get(step.value, 0) + 1


#: The steps that ask a provider to bring a machine up.
_PLANE_STARTS = frozenset({Step.NEW_ALLOCATION, Step.LAUNCH, Step.START})


async def _starts_itself(db: AsyncSession, om: OrgMachine) -> bool:
    """Whether ``om`` is a box that registers itself, by its offering's type."""
    offering = await db.get(ComputeOffering, om.offering_id)
    machine_type = await db.get(ComputeMachineType, offering.machine_type_id) if offering else None
    return machine_type is not None and provider_traits(machine_type.provider).self_registers


#: The longest one machine's step may take before it is given up on, logged
#: and left for the next pass: long enough for a launch to wait out its own
#: retry schedule across its fallbacks, short enough that one machine whose
#: provider or database stops answering cannot hold the pass (and with it every
#: other machine's step, which wait on the pass's claim) for its whole run.
STEP_BUDGET = timedelta(minutes=3)


async def allocation_history(db: AsyncSession, om: OrgMachine) -> list[ComputeAllocation]:
    """Every allocation ``om`` has had, newest first."""
    rows = await db.execute(
        select(ComputeAllocation)
        .where(
            ComputeAllocation.org_machine_id == om.id,
            ComputeAllocation.tenant_org_id == om.org_team_id,
        )
        .order_by(ComputeAllocation.created_at.desc())
    )
    return list(rows.scalars().all())


async def _locked(
    db: AsyncSession, om_id: UUID
) -> tuple[OrgMachine | None, ComputeAllocation | None]:
    # A machine whose row a request holds is left to the next pass, never
    # waited on: the pass holds every row it locks to the end of the step, and
    # a request holding the machine may be waiting on one of them.
    om = (
        await lock_rows(
            db,
            LockRank.ORG_MACHINE,
            select(OrgMachine)
            .where(OrgMachine.id == om_id)
            .execution_options(populate_existing=True),
            skip_locked=True,
        )
    ).scalar_one_or_none()
    if om is None or om.current_allocation_id is None:
        return om, None
    alloc = (
        await lock_rows(
            db,
            LockRank.ALLOCATION,
            select(ComputeAllocation)
            .where(
                ComputeAllocation.id == om.current_allocation_id,
                ComputeAllocation.tenant_org_id == om.org_team_id,
            )
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()
    return om, alloc


class OrgMachineReconciler:
    """One pass over every org machine. Holds what the pass needs: how to
    reach a provider by kind, the settings, the sleep the retry policy waits
    with, and the clock."""

    def __init__(
        self,
        *,
        providers: ProviderLookup,
        config: Settings,
        sleep: Sleep,
        now: datetime | None = None,
        only: Collection[UUID] | None = None,
        step_budget: timedelta = STEP_BUDGET,
    ) -> None:
        self._providers = providers
        self._step_budget = step_budget
        # Scope the pass to these org machines (an operator's "reconcile this
        # one now", a test that must not touch its neighbours' rows).
        self._only = frozenset(only) if only is not None else None
        self._config = config
        self._sleep = sleep
        self._now = now or datetime.now(UTC)
        self._window = RetryWindow.from_settings(config)

    async def run(self, db: AsyncSession) -> OrgReconcileSummary:
        summary = OrgReconcileSummary()
        async with cross_tenant_read(db, reason="compute.org_machines.reconcile.list") as read:
            ids = list(
                (
                    await read.execute(
                        select(OrgMachine.id)
                        .outerjoin(
                            ComputeAllocation,
                            ComputeAllocation.id == OrgMachine.current_allocation_id,
                        )
                        .where(
                            or_(
                                OrgMachine.deleted_at.is_(None),
                                ComputeAllocation.state.not_in(COMPUTE_TERMINAL_STATES),
                            )
                        )
                        .where(*self._scope())
                        .order_by(OrgMachine.created_at)
                    )
                )
                .scalars()
                .all()
            )
        for om_id in ids:
            await self._bounded(db, om_id, summary, self._one(db, om_id, summary))
        await self._idle_pass(db, summary)
        log.info(
            "compute.org_machine.reconcile",
            checked=summary.checked,
            steps=summary.steps,
            announced=summary.announced,
            idle_stops=summary.idle_stops,
            provider_errors=summary.provider_errors,
            overran=summary.overran,
        )
        return summary

    async def _bounded(
        self,
        db: AsyncSession,
        om_id: UUID,
        summary: OrgReconcileSummary,
        step: Awaitable[None],
    ) -> None:
        """Run one machine's step within :data:`STEP_BUDGET`. A step that runs
        past it is cancelled, its transaction rolled back (and every lock it
        held with it), and it is logged by name; the pass goes on to the next
        machine and the next pass tries this one again."""
        try:
            async with asyncio.timeout(self._step_budget.total_seconds()):
                await step
        except ComputeProviderError as exc:
            summary.provider_errors += 1
            log.warning(
                "compute.org_machine.provider_error", org_machine_id=str(om_id), error=str(exc)
            )
            await db.rollback()
        except TimeoutError:
            summary.overran += 1
            log.warning(
                "compute.org_machine.step_overran",
                org_machine_id=str(om_id),
                budget_seconds=self._step_budget.total_seconds(),
            )
            await db.rollback()

    def _scope(self) -> list[Any]:
        return [] if self._only is None else [OrgMachine.id.in_(self._only)]

    # -- one org machine ---------------------------------------------------------

    async def _one(self, db: AsyncSession, om_id: UUID, summary: OrgReconcileSummary) -> None:
        async with cross_tenant_write(db, reason="compute.org_machines.reconcile"):
            om, alloc = await _locked(db, om_id)
            if om is None:
                await db.rollback()
                return
            summary.checked += 1
            before = org_machine_state(om, alloc, now=self._now)
            history = await allocation_history(db, om)
            step = decide(
                om,
                alloc,
                history,
                now=self._now,
                window=self._window,
                grow=await _grow_rule(db, om, alloc),
            )
            if step in _PLANE_STARTS and await _starts_itself(db, om):
                # A box that registers itself comes back by registering
                # again: the plane has nothing to launch, start or replace.
                step = Step.NOTHING
            summary.count(step)
            if step is not Step.NOTHING:
                log.info(
                    "compute.org_machine.step",
                    org_machine_id=str(om.id),
                    allocation_id=str(alloc.id) if alloc is not None else None,
                    step=step.value,
                )
            alloc = await self._act(db, step, om, alloc)
        async with cross_tenant_write(db, reason="compute.org_machines.reconcile.announce"):
            om, alloc = await _locked(db, om_id)
            if om is not None and org_machine_state(om, alloc, now=self._now) != before:
                await announce(db, om, alloc, actor=actor_system("compute"), now=self._now)
                summary.announced += 1
            await db.commit()

    async def _act(
        self, db: AsyncSession, step: Step, om: OrgMachine, alloc: ComputeAllocation | None
    ) -> ComputeAllocation | None:
        if step is Step.NOTHING:
            await db.commit()
            return alloc
        if step is Step.NEW_ALLOCATION:
            return await self._new_allocation(db, om, alloc)
        assert alloc is not None
        machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
        provider = self._providers(machine_type.provider) if machine_type is not None else None
        if step is Step.LAUNCH:
            assert machine_type is not None and provider is not None
            await launch_allocation(
                db,
                alloc,
                machine_type,
                provider,
                org_id=om.org_team_id,
                created_by=om.created_by,
                config=self._config,
                sleep=self._sleep,
                now=self._now,
            )
        elif step is Step.START:
            await self._start(db, alloc, provider)
        elif step is Step.EXHAUSTED:
            minutes = int(self._window.capacity_retry.total_seconds() // 60)
            alloc.error = f"No hardware was free within {minutes} minutes; {alloc.error}"[:1024]
            # ``terminated_reason`` and ``failure_kind`` still say why; this
            # says the waiting is over, so it reads "couldn't start".
            alloc.capacity_gave_up_at = self._now
            await db.commit()
        elif step is Step.GIVE_UP_START:
            drop_wake(alloc, why="no hardware freed up within the capacity window")
            alloc.capacity_gave_up_at = self._now
            await db.commit()
        elif step is Step.UNDRAIN:
            transition(
                db,
                alloc,
                READY,
                reason="the org wants the machine on",
                actor=RECONCILE_ACTOR,
                now=self._now,
            )
            _clear_drain(alloc)
            await db.commit()
        elif step is Step.DRAIN:
            await self._drain(db, om, alloc)
        elif step is Step.STOP_UNSERVABLE:
            await request_power(
                db,
                om,
                desired=POWER_OFF,
                reason=STOP_NOT_RESPONDING,
                drain_kind=DRAIN_USER,
                # It serves nobody: nothing to wait for.
                deadline=self._now,
                actor=actor_system("compute.org_reconcile"),
                now=self._now,
            )
            await db.commit()
        elif step is Step.DRAIN_FOR_DISK:
            await self._drain_for_disk(db, alloc)
        elif step is Step.SLEEP:
            await sleep_org_allocation(db, alloc, now=self._now)
            await db.commit()
            if provider is not None:
                await stop_after_sleep(db, alloc.id, provider)
        elif step is Step.RELEASE:
            await self._release(db, alloc, provider)
        elif step is Step.GROW:
            assert provider is not None
            await self._grow(db, om, alloc, provider)
        return alloc

    async def _grow(
        self,
        db: AsyncSession,
        om: OrgMachine,
        alloc: ComputeAllocation,
        provider: NodeProvider,
    ) -> None:
        """Grow the machine's volume to what ``om`` asks for and price the disk
        at its new size. A machine stopped for the grow is asked to start again
        if the org wants it on. A provider that refuses is asked again next
        pass. The provider is asked with no row held (the call can take
        minutes); the rows are taken again after it and the disk priced only
        when they still ask for the size just grown to."""
        offering = await db.get(ComputeOffering, om.offering_id)
        if offering is None or not alloc.provider_machine_id:
            await db.commit()
            return
        om_id, alloc_id, machine_id, size = (
            om.id,
            alloc.id,
            alloc.provider_machine_id,
            om.storage_gb,
        )
        await db.commit()
        await provider.grow_volume(machine_id, size)
        locked_om, locked = await _locked(db, om_id)
        if locked_om is None or locked is None or locked.id != alloc_id:
            await db.commit()
            return
        om, alloc = locked_om, locked
        if om.storage_gb != size:
            # Asked to grow again meanwhile: the next pass grows to that size
            # and prices it then.
            await db.commit()
            return
        pin_disk(alloc, om, offering, now=self._now)
        if alloc.state == ASLEEP and om.desired_power == POWER_ON:
            remember_wake(
                alloc,
                actor=dict(RECONCILE_ACTOR),
                chain=actor_system("compute"),
                reason="the machine's disk grew",
                now=self._now,
            )
        await db.commit()
        log.info(
            "compute.org_machine.disk_grown",
            org_machine_id=str(om.id),
            allocation_id=str(alloc.id),
            storage_gb=om.storage_gb,
        )

    async def _new_allocation(
        self, db: AsyncSession, om: OrgMachine, old: ComputeAllocation | None
    ) -> ComputeAllocation | None:
        offering = await db.get(ComputeOffering, om.offering_id)
        machine_type = (
            await db.get(ComputeMachineType, offering.machine_type_id) if offering else None
        )
        if offering is None or machine_type is None:
            log.error("compute.org_machine.no_offering", org_machine_id=str(om.id))
            await db.commit()
            return old
        if old is not None and old.state not in (RELEASED, FAILED, LOST):
            # Never replaced from under a live machine.
            await db.commit()
            return old
        # A machine that failed after the provider started it may still be
        # there: it is ended rather than left billed and unwatched, once this
        # step has committed and holds no row.
        leftover = (
            old.provider_machine_id
            if old is not None and old.state == FAILED and old.provider_machine_id
            else None
        )
        if old is not None and old.state in (RELEASED, LOST) and not old.terminated_reason:
            old.terminated_reason = TERMINATED_REPLACED
        user_id = om.created_by or (old.user_id if old is not None else None)
        if user_id is None:
            log.error("compute.org_machine.no_owner", org_machine_id=str(om.id))
            await db.commit()
            return old
        admitted = await self._carried_rate(db, om, old, offering)
        if admitted is None:
            await db.commit()
            return old
        fresh = await new_allocation_for(
            db,
            om,
            offering=offering,
            machine_type=machine_type,
            user_id=user_id,
            admitted=admitted,
        )
        await db.commit()
        log.info(
            "compute.org_machine.new_allocation",
            org_machine_id=str(om.id),
            allocation_id=str(fresh.id),
            replaces=str(old.id) if old is not None else None,
        )
        if old is not None and leftover is not None:
            try:
                await self._providers(machine_type.provider).terminate(leftover)
            except ComputeProviderError as exc:
                log.warning(
                    "compute.org_machine.terminate_deferred",
                    allocation_id=str(old.id),
                    error=str(exc),
                )
        return fresh

    async def _carried_rate(
        self,
        db: AsyncSession,
        om: OrgMachine,
        old: ComputeAllocation | None,
        offering: ComputeOffering,
    ) -> AdmittedRate | None:
        """The rate a replacement this pass makes is priced at, or ``None``
        when it makes none. It finishes the start the org's admission let
        through (a retry after a capacity or boot failure, a machine the
        provider lost), so it carries the rate and the account that start was
        admitted at, never the offering's list price. Its account must still
        carry the start runway (``start_runway.runway_shortfall``, the check
        admission runs): when it cannot, or a granted machine's free time is
        over, the machine is stopped the way the meter stops one, and a start
        is admitted anew."""
        if old is None:
            # Every org machine is created with an admitted allocation; one
            # without has no rate anybody resolved.
            log.error("compute.org_machine.no_admitted_rate", org_machine_id=str(om.id))
            return None
        if om.acquisition == GRANTED and om.free_until is not None and om.free_until <= self._now:
            await self._stop_for(db, om, STOP_FREE_EXPIRED)
            return None
        admitted = AdmittedRate(
            rate_per_minute_nanos=old.price_per_minute_nanos,
            billing_account_id=await compute_funding().allocation_funding(db, old.id)
            or await funding_account_for(db, om),
        )
        short = await runway_shortfall(
            db,
            account_id=admitted.billing_account_id,
            owner_team_id=om.owner_team_id,
            org_id=om.org_team_id,
            need_nanos=start_runway_nanos(
                admitted.rate_per_minute_nanos, storage_price(om, offering, now=self._now)
            ),
            request_id=f"compute-replace:{om.id}:{uuid4()}",
            now=self._now,
        )
        if short is None:
            return admitted
        await self._stop_for(db, om, STOP_CREDITS)
        log.info(
            "compute.org_machine.replacement_refused",
            org_machine_id=str(om.id),
            reason=short.value,
        )
        return None

    async def _stop_for(self, db: AsyncSession, om: OrgMachine, reason: str) -> None:
        await request_power(
            db,
            om,
            desired=POWER_OFF,
            reason=reason,
            drain_kind=None,
            deadline=None,
            actor=actor_system("compute.org_reconcile"),
            now=self._now,
        )

    async def _start(
        self, db: AsyncSession, alloc: ComputeAllocation, provider: NodeProvider | None
    ) -> None:
        """Start the machine through the one wake, which holds none of its rows
        while the provider starts it, and record how the provider answered."""
        request = alloc.wake_request_json or {}
        result = await wake_allocation(
            db,
            alloc.id,
            provider,
            actor=dict(request.get("actor") or RECONCILE_ACTOR),
            chain=dict(request.get("chain") or actor_system("compute")),
            reason=str(request.get("reason") or "the org started the machine"),
            now=self._now,
            # A start already asked for keeps its time: the capacity window
            # runs from the ask, not from this pass.
            retry=True,
        )
        if result.refused is not None:
            # Kept asleep with the start still asked for: a host with no free
            # GPU reads "waiting for hardware", and the next pass asks again.
            await self._record_start(db, alloc.id, failure_kind=result.refused.kind, started=False)
            log.info(
                "compute.org_machine.start_refused",
                allocation_id=str(alloc.id),
                kind=result.refused.kind,
                error=str(result.refused)[:300],
            )
            return
        if result.started:
            await self._record_start(db, alloc.id, failure_kind="", started=True)

    async def _record_start(
        self, db: AsyncSession, allocation_id: UUID, *, failure_kind: str, started: bool
    ) -> None:
        async with cross_tenant_write(db, reason="compute.org_machines.reconcile.start"):
            alloc = (
                await lock_rows(
                    db,
                    LockRank.ALLOCATION,
                    select(ComputeAllocation)
                    .where(ComputeAllocation.id == allocation_id)
                    .execution_options(populate_existing=True),
                )
            ).scalar_one_or_none()
            if alloc is not None:
                alloc.failure_kind = failure_kind
                if started:
                    alloc.capacity_gave_up_at = None
            await db.commit()

    async def _drain(self, db: AsyncSession, om: OrgMachine, alloc: ComputeAllocation) -> None:
        deleting = om.deleted_at is not None
        transition(
            db,
            alloc,
            DRAINING,
            reason="the machine is being deleted" if deleting else "the org stopped the machine",
            actor=RECONCILE_ACTOR,
            now=self._now,
        )
        alloc.drain_requested_at = self._now
        alloc.drain_kind = (
            DRAIN_USER if deleting else _DRAIN_FOR_STOP.get(om.stop_reason, DRAIN_USER)
        )
        alloc.drain_reason = "delete" if deleting else (om.stop_reason or "stop")
        alloc.drain_deadline_at = self._now + DELETE_DRAIN if deleting else self._now
        # A deleted machine's drain ends in a release: the node reconcile
        # releases it the moment it is empty, this pass at its deadline.
        alloc.auto_terminate = deleting
        await announce_machine(
            db, alloc, status=DRAINING_STATUS, reason=None, actor=actor_system("compute")
        )
        await db.commit()

    async def _drain_for_disk(self, db: AsyncSession, alloc: ComputeAllocation) -> None:
        """Stop taking chats so the disk can grow: the turns running get the
        move grace to finish, never a hard kill."""
        transition(
            db,
            alloc,
            DRAINING,
            reason="the org grew the machine's disk",
            actor=RECONCILE_ACTOR,
            now=self._now,
        )
        alloc.drain_requested_at = self._now
        alloc.drain_kind = DRAIN_USER
        alloc.drain_reason = "disk"
        alloc.drain_deadline_at = self._now + timedelta(
            seconds=self._config.move_turn_grace_seconds
        )
        alloc.auto_terminate = False
        await announce_machine(
            db, alloc, status=DRAINING_STATUS, reason=None, actor=actor_system("compute")
        )
        await db.commit()

    async def _release(
        self, db: AsyncSession, alloc: ComputeAllocation, provider: NodeProvider | None
    ) -> None:
        """Release the machine for good: terminate it at the provider, its
        volume with it, and take back its credential and node token."""
        if alloc.state == PENDING:
            alloc.terminated_reason = alloc.terminated_reason or "user_released"
            transition(
                db,
                alloc,
                FAILED,
                reason="the machine was deleted before it started",
                actor=RECONCILE_ACTOR,
                now=self._now,
            )
            await revoke_node_credentials(db, alloc, now=self._now)
            await db.commit()
            return
        transition(
            db,
            alloc,
            RELEASING,
            reason="the machine was deleted",
            actor=RECONCILE_ACTOR,
            now=self._now,
        )
        alloc.terminated_reason = alloc.terminated_reason or "user_released"
        _clear_drain(alloc)
        alloc.wake_requested_at = None
        alloc.wake_request_json = None
        await revoke_node_credentials(db, alloc, now=self._now)
        await leave_placement(db, alloc, actor=actor_system("compute"))
        await db.commit()
        if provider is None:
            return
        try:
            if alloc.provider_machine_id:
                await provider.terminate(alloc.provider_machine_id)
            await settle_release(db, alloc.id, provider, now=self._now)
        except ComputeProviderError as exc:
            # The node reconcile finishes a release that did not land.
            log.warning(
                "compute.org_machine.release_deferred", allocation_id=str(alloc.id), error=str(exc)
            )

    # -- idle stops, per org -----------------------------------------------------

    async def _idle_pass(self, db: AsyncSession, summary: OrgReconcileSummary) -> None:
        """Ask every running org machine idle past its window to stop; an org's
        pool machines keep the org's ``min_awake_pool`` of them awake."""
        async with cross_tenant_read(db, reason="compute.org_machines.reconcile.idle") as read:
            rows = (
                await read.execute(
                    select(OrgMachine, ComputeAllocation, ComputeOffering.idle_stop_minutes_default)
                    .join(
                        ComputeAllocation, ComputeAllocation.id == OrgMachine.current_allocation_id
                    )
                    .join(ComputeOffering, ComputeOffering.id == OrgMachine.offering_id)
                    .where(
                        OrgMachine.deleted_at.is_(None),
                        OrgMachine.desired_power == POWER_ON,
                        ComputeAllocation.state == READY,
                        ComputeAllocation.tenant_org_id == OrgMachine.org_team_id,
                        *self._scope(),
                    )
                )
            ).all()
            minimums = {
                org_id: minimum
                for org_id, minimum in (
                    await read.execute(
                        select(OrgComputeSettings.org_team_id, OrgComputeSettings.min_awake_pool)
                    )
                ).all()
            }
            # Decided while the read is open: its rows expire when it ends.
            pools: dict[UUID, list[PoolMachine]] = defaultdict(list)
            stop: list[UUID] = []
            empty = await _audience_empty(read, [om.id for om, _alloc, _default in rows])
            for om, alloc, offering_default in rows:
                idle = is_idle(
                    om,
                    alloc,
                    self._now,
                    audience_empty=om.id in empty,
                    offering_default=offering_default,
                )
                if om.use_mode == USE_POOL:
                    pools[om.org_team_id].append(
                        PoolMachine(org_machine=om, allocation=alloc, idle=idle)
                    )
                elif idle:
                    stop.append(om.id)
            for org_id, machines in pools.items():
                stop.extend(
                    m.id for m in pool_machines_to_stop(machines, min_awake=minimums.get(org_id, 0))
                )
        for om_id in stop:
            await self._bounded(db, om_id, summary, self._idle_stop(db, om_id, summary))

    async def _idle_stop(self, db: AsyncSession, om_id: UUID, summary: OrgReconcileSummary) -> None:
        async with cross_tenant_write(db, reason="compute.org_machines.reconcile.idle_stop"):
            om, alloc = await _locked(db, om_id)
            # Re-checked under the lock: a chat that started work since the
            # read keeps its machine.
            if om is None or om.desired_power != POWER_ON:
                await db.rollback()
                return
            offering = await db.get(ComputeOffering, om.offering_id)
            if not is_idle(
                om,
                alloc,
                self._now,
                audience_empty=om.id in await _audience_empty(db, [om.id]),
                offering_default=offering.idle_stop_minutes_default if offering else None,
            ):
                await db.rollback()
                return
            await request_power(
                db,
                om,
                desired=POWER_OFF,
                reason=STOP_IDLE,
                drain_kind=DRAIN_IDLE,
                deadline=self._now,
                actor=actor_system("compute"),
            )
            await db.commit()
            summary.idle_stops += 1
            log.info("compute.org_machine.idle_stop", org_machine_id=str(om_id))


async def _audience_empty(db: AsyncSession, om_ids: Sequence[UUID]) -> set[UUID]:
    """Which of ``om_ids`` nobody may use: no audience row of any kind."""
    if not om_ids:
        return set()
    granted = set(
        (
            await db.execute(
                select(OrgMachineAudience.org_machine_id)
                .where(OrgMachineAudience.org_machine_id.in_(om_ids))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return set(om_ids) - granted


def _clear_drain(alloc: ComputeAllocation) -> None:
    alloc.drain_requested_at = None
    alloc.drain_reason = ""
    alloc.drain_kind = None
    alloc.drain_deadline_at = None
    alloc.auto_terminate = False


async def reconcile_org_machines(
    db: AsyncSession,
    *,
    providers: ProviderLookup,
    config: Settings,
    sleep: Sleep,
    now: datetime | None = None,
    only: Collection[UUID] | None = None,
    step_budget: timedelta = STEP_BUDGET,
) -> OrgReconcileSummary:
    """One pass over every org machine (the module docstring), or over the
    ones named in ``only``. Each machine's step is bounded by ``step_budget``."""
    return await OrgMachineReconciler(
        providers=providers,
        config=config,
        sleep=sleep,
        now=now,
        only=only,
        step_budget=step_budget,
    ).run(db)


__all__ = [
    "DELETE_DRAIN",
    "RETRIED_FAILURE_KINDS",
    "STEP_BUDGET",
    "STOP_DRAIN_KINDS",
    "CapacityWait",
    "OrgMachineReconciler",
    "OrgReconcileSummary",
    "RetryWindow",
    "Step",
    "allocation_history",
    "capacity_wait",
    "decide",
    "reconcile_org_machines",
]
