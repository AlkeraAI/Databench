"""Every minute, make each provisioned machine's row say what its provider says.

For every ``provisioned`` workspace machine not yet ``released`` / ``failed``,
the pass asks its node provider to ``describe`` it and moves the row along
exactly one lifecycle edge through
:func:`~alkera_core.compute.transitions.transition`:

======================  ==============================  ===================================
row                     provider / clock                move
======================  ==============================  ===================================
provisioning            running                         bootstrapping
provisioning, no id     the provider finds it by tag    (the id is recorded, then as below)
provisioning            not found, under 5 min          (nothing: not visible yet)
provisioning            older than 10 min / gone        failed (+ best-effort terminate)
bootstrapping           older than 15 min / gone        failed (+ best-effort terminate)
                        (a timeout is ``boot_failed``; a machine gone is ``provider_error``)
bootstrapping           the daemon claims               ready (the claim route does this)
ready / draining        gone                            lost, then released
asleep                  gone (not merely stopped)       lost, then released
draining                empty and ``auto_terminate``    releasing (+ terminate)
releasing               gone                            released
releasing               still there                     terminate again
asleep, wake waiting    the provider takes the start    ready (as whoever asked for the wake)
asleep, wake waiting    older than 15 min               (the request is dropped)
======================  ==============================  ===================================

A row that leaves the plane (released or failed) gives back its node secret
and its machine credential, drops the org assignment that named it, and
strands the chats still bound to it (:mod:`alkera_core.compute.handoff`), in
the same transaction as the edge, so no reader ever sees a ``ready`` chat on a
machine that is gone. The next box to come up, or each chat's next message,
places them again.

No row is locked while a provider is asked anything. Each machine is
observed first (``find``, ``describe``), with nothing held; then its row is
locked, checked to be where it was when observed (a claim or an admin click in
between leaves it for the next pass), moved, and committed; and only then are
the provider's acts asked for (terminate, stop, start, the secret's binding and
deletion), best effort. A heartbeat, a message and a claim on the machine never
wait behind a provider's answer, and a terminate that a crash never asked is
the orphan sweep's (:mod:`alkera_core.compute.reconcile`).

Whatever went wrong between ticks (a backend restart mid-provision, a box that
died, a terminate that never answered), the next pass converges it. A provider
that cannot be asked (``ComputeProviderError``) leaves its rows for the next
pass rather than guessing, and logs once per machine when the outage starts
and once when it ends, not once per pass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.revocation import revoke_jti
from alkera_core.compute.boot_failure import never_claimed_error
from alkera_core.compute.events import SYSTEM_ACTOR as SYSTEM_ACTOR_LABEL
from alkera_core.compute.handoff import leave_placement
from alkera_core.compute.machines import PLACE_AGAIN
from alkera_core.compute.nodes import NodeDescription, NodeProvider
from alkera_core.compute.power_lock import stop_after_sleep
from alkera_core.compute.provider import GONE, RUNNING, ComputeProviderError
from alkera_core.compute.transitions import transition
from alkera_core.compute.wake import complete_wake, drop_wake, wake_expired
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import actor_system
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    ASLEEP,
    BOOTSTRAPPING,
    DEDICATED_TENANCY,
    DRAINING,
    FAILED,
    LOST,
    PENDING,
    POOL_TENANCY,
    PROVISIONED,
    PROVISIONING,
    READY,
    RELEASED,
    RELEASING,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.machine_credential import MachineCredential

log = get_logger(__name__)

PROVISIONING_TIMEOUT = timedelta(minutes=10)

#: How long a machine still ``provisioning`` may be unknown to its provider
#: before that is read as gone. EC2's view of a new instance is eventually
#: consistent; a ``not-found`` inside this window is "not visible yet".
NOT_YET_VISIBLE = timedelta(minutes=5)

#: The ``raw_status`` a node provider reports for a machine it has no record of.
NOT_FOUND = "not-found"
BOOTSTRAPPING_TIMEOUT = timedelta(minutes=15)

#: The ``terminated_reason`` of a machine that did not come up within its boot
#: timeouts (provisioning or bootstrapping too long). Distinct from one the
#: provider lost, because a boot failure is worth one try on fresh hardware.
BOOT_FAILED = "boot_failed"

SYSTEM_ACTOR: dict[str, Any] = {"kind": "system", "email": None, "name": "reconcile"}

NodeProviderLookup = Callable[[str], NodeProvider]


class ProviderErrorLog:
    """Which machines the pass has already said it could not ask about.

    The pass runs every minute, so a provider that stays down for an hour
    would otherwise write the same warning sixty times per machine and bury
    the one line that mattered. A machine is reported when it first becomes
    unaskable and again only after it has answered in between. Process memory
    is the right store: the question is what THIS worker has already said."""

    def __init__(self) -> None:
        self._failing: set[UUID] = set()

    def failed(self, allocation_id: UUID) -> bool:
        """Note a failure; True when this is news worth a warning."""
        if allocation_id in self._failing:
            return False
        self._failing.add(allocation_id)
        return True

    def answered(self, allocation_id: UUID) -> bool:
        """Note an answer; True when the machine had been reported failing."""
        if allocation_id not in self._failing:
            return False
        self._failing.discard(allocation_id)
        return True


PROVIDER_ERRORS = ProviderErrorLog()
"""The worker's own record, used when a caller does not pass one."""


@dataclass
class NodeReconcileSummary:
    checked: int = 0
    moved: int = 0
    provider_errors: int = 0
    edges: list[tuple[str, str]] = field(default_factory=list)


Act = Callable[[], Awaitable[None]]
"""A provider call decided under the row's lock, asked after its commit."""


@dataclass(frozen=True, slots=True)
class _Observed:
    """What the provider said about one machine, asked with nothing held."""

    #: The machine found by the allocation it was started for, when the row
    #: never learned its id.
    found: NodeDescription | None
    described: NodeDescription | None


async def reconcile_nodes(
    db: AsyncSession,
    *,
    providers: NodeProviderLookup,
    now: datetime | None = None,
    errors: ProviderErrorLog | None = None,
) -> NodeReconcileSummary:
    moment = now or datetime.now(UTC)
    said = errors if errors is not None else PROVIDER_ERRORS
    rows = (
        await db.execute(
            select(ComputeAllocation.id, ComputeMachineType.provider)
            .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
            .where(
                ComputeAllocation.origin == PROVISIONED,
                ComputeAllocation.lifecycle == "workspace",
                # A pending row has asked no provider for anything yet: there
                # is nothing to describe or find, and an org machine's pending
                # row is the org-machine reconcile's to launch.
                ComputeAllocation.state.not_in((RELEASED, FAILED, PENDING)),
            )
            .order_by(ComputeAllocation.created_at)
        )
    ).all()
    await db.commit()
    summary = NodeReconcileSummary()
    for allocation_id, kind in rows:
        provider = providers(kind)
        seen = await db.get(ComputeAllocation, allocation_id, populate_existing=True)
        await db.commit()
        if seen is None or seen.state in (RELEASED, FAILED, PENDING):
            continue
        summary.checked += 1
        before, machine_id = seen.state, seen.provider_machine_id
        if _wake_lapsed(seen, now=moment):
            # Dropped before the provider is asked: a provider that cannot answer
            # at all must not keep the request alive past its bound.
            await _drop_lapsed_wake(db, allocation_id, now=moment)
            continue
        try:
            observed = await _observe(seen, provider)
        except ComputeProviderError as exc:
            summary.provider_errors += 1
            if said.failed(allocation_id):
                log.warning(
                    "compute.nodes.provider_error",
                    allocation_id=str(allocation_id),
                    error=str(exc),
                )
            continue
        if said.answered(allocation_id):
            log.info("compute.nodes.provider_answered", allocation_id=str(allocation_id))
        # The row is read fresh under its lock and moved only if it is where it
        # was when the provider was asked: a claim or an admin click that raced
        # this pass is seen, not overwritten, and the next pass looks again.
        alloc = await _locked(db, allocation_id)
        if alloc is None or alloc.state != before or alloc.provider_machine_id != machine_id:
            await db.rollback()
            continue
        acts = await _one(db, alloc, provider, observed, now=moment)
        after = alloc.state
        await db.commit()
        for act in acts:
            await act()
        if after != before:
            summary.moved += 1
            summary.edges.append((before, after))
    return summary


async def _locked(db: AsyncSession, allocation_id: UUID) -> ComputeAllocation | None:
    return (
        await lock_rows(
            db,
            LockRank.ALLOCATION,
            select(ComputeAllocation)
            .where(ComputeAllocation.id == allocation_id)
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()


def _wake_lapsed(alloc: ComputeAllocation, *, now: datetime) -> bool:
    return alloc.state == ASLEEP and alloc.org_machine_id is None and wake_expired(alloc, now=now)


async def _drop_lapsed_wake(db: AsyncSession, allocation_id: UUID, *, now: datetime) -> None:
    alloc = await _locked(db, allocation_id)
    if alloc is not None and _wake_lapsed(alloc, now=now):
        drop_wake(alloc, why="the provider did not allow the start within the bound")
    await db.commit()


async def _observe(alloc: ComputeAllocation, provider: NodeProvider) -> _Observed:
    """Ask the provider about the machine, holding nothing."""
    if alloc.state == LOST:
        return _Observed(found=None, described=None)
    found = None
    machine_id = alloc.provider_machine_id
    if not machine_id:
        # The create's answer never reached the row (a timed-out RunInstances,
        # a request cancelled before its commit). The machine may still exist,
        # and a row that times out without its id cannot terminate it, so ask
        # the provider for it by the allocation it was started for.
        found = await provider.find(alloc.id)
        machine_id = found.machine_id if found is not None else ""
    described = await provider.describe(machine_id) if machine_id else None
    return _Observed(found=found, described=described)


def _age(alloc: ComputeAllocation, now: datetime) -> timedelta:
    return now - (alloc.state_changed_at or alloc.created_at)


async def _one(
    db: AsyncSession,
    alloc: ComputeAllocation,
    provider: NodeProvider,
    observed: _Observed,
    *,
    now: datetime,
) -> list[Act]:
    """Move the locked row as the observation says; return the provider acts
    to ask once it has committed."""
    acts: list[Act] = []
    if alloc.state == LOST:
        acts += await _leave(db, alloc, provider, RELEASED, "chats handed on", now=now)
        return acts
    if observed.found is not None:
        alloc.provider_machine_id = observed.found.machine_id
        log.warning(
            "compute.nodes.adopted",
            allocation_id=str(alloc.id),
            machine_id=observed.found.machine_id,
        )
        # Provision stopped before it tied the secret to the machine; tie it
        # now so the box can still boot. If it cannot be tied, the box never
        # claims and the boot timeout below ends it.
        acts.append(_bind(alloc.id, observed.found.machine_id, provider))
    described = observed.described
    phase = described.phase if described is not None else None
    if (
        alloc.state == PROVISIONING
        and described is not None
        and described.raw_status == NOT_FOUND
        and _age(alloc, now) < NOT_YET_VISIBLE
    ):
        # EC2 answers InvalidInstanceID.NotFound for a while after
        # RunInstances. A machine that has never been seen running is not
        # dead because the provider cannot see it yet; one the provider
        # reports terminated is, and still fails at once.
        phase = None
    if alloc.state == PROVISIONING:
        if phase == RUNNING:
            transition(
                db,
                alloc,
                BOOTSTRAPPING,
                reason="provider reports running",
                actor=SYSTEM_ACTOR,
                now=now,
            )
        elif phase == GONE:
            acts += await _fail(
                db, alloc, provider, "the provider no longer has the machine", now=now
            )
        elif _age(alloc, now) > PROVISIONING_TIMEOUT:
            acts += await _fail(
                db, alloc, provider, "provisioning timed out", now=now, terminated=BOOT_FAILED
            )
    elif alloc.state == BOOTSTRAPPING:
        if phase == GONE:
            acts += await _fail(
                db, alloc, provider, "the provider no longer has the machine", now=now
            )
        elif _age(alloc, now) > BOOTSTRAPPING_TIMEOUT:
            # The node's own last error, when its provider could read one, is
            # what the machine card shows as the reason.
            acts += await _fail(
                db,
                alloc,
                provider,
                never_claimed_error(described.note if described is not None else ""),
                now=now,
                terminated=BOOT_FAILED,
            )
    elif alloc.state in (READY, DRAINING):
        if phase == GONE:
            transition(
                db,
                alloc,
                LOST,
                reason="the provider no longer has the machine",
                actor=SYSTEM_ACTOR,
                now=now,
            )
            alloc.terminated_reason = alloc.terminated_reason or "provider_gone"
            acts += await _leave(db, alloc, provider, RELEASED, "chats handed on", now=now)
            acts.append(_offer_stranded_chats(db, alloc.id))
        elif alloc.state == DRAINING and alloc.chats_served == 0 and alloc.auto_terminate:
            transition(db, alloc, RELEASING, reason="drained empty", actor=SYSTEM_ACTOR, now=now)
            alloc.terminated_reason = alloc.terminated_reason or "user_released"
            await _revoke(db, alloc, now=now)
            acts.append(_terminate(alloc, provider))
    elif alloc.state == ASLEEP:
        # Stopped on purpose and kept: never billed as compute (asleep is not a
        # metered state) and never released here. Providers report a stopped
        # machine as ``stopped``, distinct from ``gone``, so only a machine the
        # provider no longer has at all (terminated under us, a 404) is lost;
        # its org's next start replaces it. If it is somehow still RUNNING, the
        # sleep's stop never landed: try again so it actually stops costing —
        # unless a wake is waiting on it, which the start completes.
        if phase == GONE:
            transition(
                db,
                alloc,
                LOST,
                reason="the provider no longer has the stopped machine",
                actor=SYSTEM_ACTOR,
                now=now,
            )
            alloc.terminated_reason = alloc.terminated_reason or "provider_gone"
            alloc.wake_requested_at = None
            alloc.wake_request_json = None
            acts += await _leave(db, alloc, provider, RELEASED, "chats handed on", now=now)
        elif alloc.wake_requested_at is not None:
            # An org machine's start is the org-machine reconcile's: it keeps a
            # start the provider refused for lack of hardware for its whole
            # capacity window, rather than this pass's fifteen minutes.
            if alloc.org_machine_id is None:
                acts.append(_complete_wake(db, alloc.id, provider, now=now))
        elif phase == RUNNING:
            acts.append(_stop(db, alloc.id, provider))
    elif alloc.state == RELEASING:
        if phase == GONE or not alloc.provider_machine_id:
            note = described.note if described is not None else ""
            if note:
                alloc.error = note
            acts += await _leave(
                db,
                alloc,
                provider,
                release_end(alloc),
                note or "the provider released the machine",
                now=now,
            )
        else:
            acts.append(_terminate(alloc, provider))
    return acts


def release_end(alloc: ComputeAllocation) -> str:
    """Where a finished release lands: a release somebody asked for ends
    ``released``; a stop the plane imposed (the meter's, under its own reason)
    ends ``failed``."""
    return RELEASED if alloc.terminated_reason in ("", "user_released") else FAILED


async def settle_release(
    db: AsyncSession,
    allocation_id: UUID,
    provider: NodeProvider,
    *,
    now: datetime | None = None,
) -> str | None:
    """Finish a ``releasing`` machine now, if its provider confirms it gone.

    The terminate path calls this right after the provider accepted the
    terminate, so a machine an admin released reads ``released`` on the next
    read rather than after the next reconcile pass. It is the same edge the
    pass takes (:func:`release_end`, the secret and every credential given
    back) and the pass stays the backstop: a provider that still reports the
    machine, or that cannot be asked (``ComputeProviderError`` is raised to
    the caller), leaves the row ``releasing`` for it.

    The provider is asked BEFORE the row is locked, so no lock is held across
    a network call; the row is then re-read under its lock and moved only if
    it is still ``releasing`` on the same machine — a pass that finished it in
    between is seen, not repeated. The secret is deleted after the commit.
    Commits; returns the row's state after, or ``None`` for a row that does
    not exist."""
    moment = now or datetime.now(UTC)
    before = await db.get(ComputeAllocation, allocation_id, populate_existing=True)
    if before is None:
        await db.commit()
        return None
    machine_id = before.provider_machine_id
    if before.state != RELEASING:
        await db.commit()
        return before.state
    await db.commit()
    if machine_id:
        described = await provider.describe(machine_id)
        if described.phase != GONE:
            return RELEASING
    alloc = await _locked(db, allocation_id)
    if alloc is None or alloc.state != RELEASING or alloc.provider_machine_id != machine_id:
        await db.rollback()
        return alloc.state if alloc is not None else None
    acts = await _leave(
        db, alloc, provider, release_end(alloc), "the provider released the machine", now=moment
    )
    state = alloc.state
    await db.commit()
    for act in acts:
        await act()
    return state


async def _fail(
    db: AsyncSession,
    alloc: ComputeAllocation,
    provider: NodeProvider,
    reason: str,
    *,
    now: datetime,
    terminated: str = "provider_error",
) -> list[Act]:
    alloc.error = reason
    alloc.terminated_reason = alloc.terminated_reason or terminated
    terminate = _terminate(alloc, provider)
    return [terminate, *await _leave(db, alloc, provider, FAILED, reason, now=now)]


async def _leave(
    db: AsyncSession,
    alloc: ComputeAllocation,
    provider: NodeProvider,
    to: str,
    reason: str,
    *,
    now: datetime,
) -> list[Act]:
    transition(db, alloc, to, reason=reason, actor=SYSTEM_ACTOR, now=now)
    await _revoke(db, alloc, now=now)
    # The machine is off the plane in this same transaction: the org it was
    # dedicated to places by the ordinary rules again, and every chat still
    # bound to it says it is waiting rather than ``ready`` on a box that is
    # gone. The backend's terminate already did this at ``releasing``, so for
    # a release it began this is a no-op; for a loss the worker found, it is
    # the only place it happens.
    await leave_placement(db, alloc, actor=actor_system(SYSTEM_ACTOR_LABEL))
    return [_delete_credential(alloc.id, provider)]


def _offer_stranded_chats(db: AsyncSession, lost_id: UUID) -> Act:
    """Make every other live platform box place the chats the lost one held.

    A chat whose reader sends another message is placed again on that message,
    but one that is waiting on the answer the lost box was writing has no next
    message coming, and a box binds stranded chats only as it comes up. So the
    boxes still standing are told to announce themselves again: marking what
    each last reported as :data:`PLACE_AGAIN` makes its next heartbeat the ready
    transition that runs placement over every stranded chat — through the same
    eligibility and tenancy rules as any box coming up, within one heartbeat
    interval.

    In a transaction of its own, after the loss has committed: the statement
    touches every ready box's row in the fleet, and holding them all until the
    end of the loss's work would hold up every one of their heartbeats."""

    async def act() -> None:
        await db.execute(
            update(ComputeAllocation)
            .where(
                ComputeAllocation.id != lost_id,
                ComputeAllocation.lifecycle == "workspace",
                ComputeAllocation.state == READY,
                ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)),
            )
            .values(last_reported_status=PLACE_AGAIN)
        )
        await db.commit()

    return act


async def revoke_node_credentials(
    db: AsyncSession, alloc: ComputeAllocation, *, now: datetime
) -> None:
    """Revoke everything the machine was handed to authenticate with: its
    machine credential and, when it was given one, its own CLI login (by the
    token's ``jti``). Every way a provisioned machine leaves the plane ends
    here, so a box that outlives its row — a terminate that never landed, a
    disk someone snapshotted — holds nothing the API still accepts."""
    await db.execute(
        update(MachineCredential)
        .where(MachineCredential.machine_id == alloc.id, MachineCredential.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if alloc.node_token_jti:
        await revoke_jti(db, alloc.node_token_jti)


async def _revoke(db: AsyncSession, alloc: ComputeAllocation, *, now: datetime) -> None:
    await revoke_node_credentials(db, alloc, now=now)


def _terminate(alloc: ComputeAllocation, provider: NodeProvider) -> Act:
    machine_id, allocation_id = alloc.provider_machine_id, alloc.id

    async def act() -> None:
        if not machine_id:
            return
        try:
            await provider.terminate(machine_id)
        except ComputeProviderError as exc:
            log.warning(
                "compute.nodes.terminate_deferred", allocation_id=str(allocation_id), error=str(exc)
            )

    return act


def _delete_credential(allocation_id: UUID, provider: NodeProvider) -> Act:
    async def act() -> None:
        try:
            await provider.delete_credential(allocation_id)
        except ComputeProviderError as exc:
            log.warning(
                "compute.nodes.secret_leftover", allocation_id=str(allocation_id), error=str(exc)
            )

    return act


def _bind(allocation_id: UUID, machine_id: str, provider: NodeProvider) -> Act:
    async def act() -> None:
        try:
            await provider.bind_credential(allocation_id, machine_id)
        except ComputeProviderError as exc:
            log.warning(
                "compute.nodes.bind_failed", allocation_id=str(allocation_id), error=str(exc)
            )

    return act


def _stop(db: AsyncSession, allocation_id: UUID, provider: NodeProvider) -> Act:
    """The sleep's stop asked again, under the machine's power claim."""

    async def act() -> None:
        await stop_after_sleep(db, allocation_id, provider)

    return act


def _complete_wake(
    db: AsyncSession, allocation_id: UUID, provider: NodeProvider, *, now: datetime
) -> Act:
    async def act() -> None:
        await complete_wake(db, allocation_id, provider, now=now)

    return act


__all__ = [
    "BOOTSTRAPPING_TIMEOUT",
    "BOOT_FAILED",
    "NOT_FOUND",
    "NOT_YET_VISIBLE",
    "PROVIDER_ERRORS",
    "PROVISIONING_TIMEOUT",
    "SYSTEM_ACTOR",
    "NodeReconcileSummary",
    "ProviderErrorLog",
    "reconcile_nodes",
    "release_end",
    "revoke_node_credentials",
    "settle_release",
]
