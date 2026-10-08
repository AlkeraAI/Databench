"""Starting a sleeping machine, and the wake a provider refused.

:func:`wake_allocation` is the one wake: the admin's wake, a message to a chat
on a sleeping box, the org-machine reconcile starting an org machine and the
node reconcile completing a refused wake all go through it, so each writes
the same history edge, re-anchors the meter the same way and tells the same
readers the box is coming up.

It never holds the machine's rows while the provider starts it. The caller's
transaction ends first; the wake is recorded on the row in a short
transaction of its own; the provider is asked under the machine's power claim
alone (:mod:`alkera_core.compute.power_lock`); and the row moves to ``ready``
in one more short transaction, only if it is still asleep with the wake on it.
A start takes seconds to minutes at a provider, and a message to one of the
machine's chats, its box's heartbeats and the reconcile must not wait for it.

A provider can refuse a start while it is still stopping the machine — EC2
answers ``IncorrectInstanceState`` until ``StopInstances`` has finished, about
a minute after a sleep. A wake refused that way is not dropped: it is already
on the row (:func:`remember_wake`), and the node reconcile calls
:func:`complete_wake` every pass until the provider takes the start, or the
request is older than :data:`WAKE_REQUEST_TTL` and is dropped with a logged
reason, so a provider that never answers cannot hold a row forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.events import announce_machine
from alkera_core.compute.handoff import restate_bound_chats
from alkera_core.compute.machines import STARTING
from alkera_core.compute.nodes import NodeProvider
from alkera_core.compute.power_lock import StopInFlightError, power_claim
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.compute.transitions import transition
from alkera_core.db.locking import LockRank, claimed_io, lock_rows
from alkera_core.logging import get_logger
from alkera_core.models.compute import ASLEEP, PROVISIONED, READY, ComputeAllocation

log = get_logger(__name__)

#: How long a refused wake is kept for the reconcile to complete. EC2 finishes
#: a stop in about a minute; a request this old is waiting on a provider that
#: is not going to answer, and a person who still wants the box sends again.
WAKE_REQUEST_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class WakeResult:
    """What a wake did.

    ``started`` when the row moved to ``ready``. Otherwise ``state`` is what
    the row was found in (a machine that was not asleep is not woken), or
    ``refused`` is the provider's refusal, the wake left on the row for the
    reconcile to complete.
    """

    started: bool
    state: str
    refused: ComputeProviderError | None = None


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


async def wake_allocation(
    db: AsyncSession,
    allocation_id: UUID,
    provider: NodeProvider | None,
    *,
    actor: dict[str, Any],
    chain: dict[str, Any],
    reason: str,
    now: datetime | None = None,
    retry: bool = False,
) -> WakeResult:
    """Start the provider machine, then move the row to ``ready`` and
    re-anchor the meter at now, holding none of the machine's rows while the
    provider works.

    Ends the caller's transaction first (a commit): whatever it held is
    released, and whatever it wrote is kept. The heartbeat stamp is cleared so
    the box reads ``starting`` — not ``unreachable`` off a stamp from before
    the sleep — until its daemon beats, its readings from before the sleep are
    dropped, and the chats parked on it are restated and announced the same
    way, so their readers see the box coming up.

    ``provider`` is None for a machine the plane did not provision: there is
    nothing to start, only the row to move. Never raises for the provider: a
    refusal (:class:`~alkera_core.compute.power_lock.StopInFlightError` while
    the sleep's stop is still being asked) comes back in the result, with the
    wake left on the row. ``retry`` is the reconcile trying a wake already on
    the row again: the request is left as it was asked, so its bound runs
    from the ask and not from the latest try. Commits."""
    moment = now or datetime.now(UTC)
    await db.commit()
    alloc = await _locked(db, allocation_id)
    if alloc is None or alloc.state != ASLEEP:
        state = alloc.state if alloc is not None else ""
        await db.rollback()
        return WakeResult(started=False, state=state)
    if not (retry and alloc.wake_requested_at is not None):
        remember_wake(alloc, actor=actor, chain=chain, reason=reason, now=moment)
    machine_id = alloc.provider_machine_id
    asks_provider = provider is not None and bool(machine_id) and alloc.origin == PROVISIONED
    await db.commit()
    async with claimed_io(db, power_claim(allocation_id)) as claimed:
        if asks_provider:
            assert provider is not None and machine_id is not None
            if not claimed:
                return WakeResult(started=False, state=ASLEEP, refused=StopInFlightError())
            try:
                await provider.start(machine_id)
            except ComputeProviderError as exc:
                return WakeResult(started=False, state=ASLEEP, refused=exc)
        alloc = await _locked(db, allocation_id)
        if alloc is None or alloc.state != ASLEEP or alloc.wake_requested_at is None:
            # Another wake finished it, or it left the plane meanwhile.
            return WakeResult(started=False, state=alloc.state if alloc is not None else "")
        await _mark_started(db, alloc, reason=reason, actor=actor, chain=chain, now=moment)
        return WakeResult(started=True, state=READY)


async def _mark_started(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    reason: str,
    actor: dict[str, Any],
    chain: dict[str, Any],
    now: datetime,
) -> None:
    transition(db, alloc, READY, reason=reason, actor=actor, now=now)
    # Resume billing from NOW, never from before the sleep — the stopped window
    # cost no compute and must not be billed as whole minutes on the next tick.
    alloc.last_metered_at = now
    alloc.last_heartbeat_at = None
    # The readings the box sent before the sleep describe a machine that has
    # since been stopped; the first heartbeat after the wake brings new ones.
    alloc.resources_json = None
    alloc.wake_requested_at = None
    alloc.wake_request_json = None
    await announce_machine(db, alloc, status=STARTING, reason=None, actor=chain)
    await restate_bound_chats(db, machine_id=alloc.id, status="starting", actor=chain)


def remember_wake(
    alloc: ComputeAllocation,
    *,
    actor: dict[str, Any],
    chain: dict[str, Any],
    reason: str,
    now: datetime | None = None,
) -> None:
    """Record a wake on the row, for the reconcile to complete if the start
    does not land.

    A later request replaces an earlier one: the newest person asking is the
    one the start is recorded for, and their ask restarts the bound."""
    alloc.wake_requested_at = now or datetime.now(UTC)
    alloc.wake_request_json = {"actor": actor, "chain": chain, "reason": reason}


def wake_expired(alloc: ComputeAllocation, *, now: datetime) -> bool:
    """Whether the wake waiting on the row is past its bound."""
    requested = alloc.wake_requested_at
    return requested is not None and now - requested > WAKE_REQUEST_TTL


def drop_wake(alloc: ComputeAllocation, *, why: str) -> None:
    """Forget the waiting wake, saying why."""
    log.warning(
        "compute.wake.dropped",
        allocation_id=str(alloc.id),
        requested_at=alloc.wake_requested_at.isoformat() if alloc.wake_requested_at else None,
        reason=why,
    )
    alloc.wake_requested_at = None
    alloc.wake_request_json = None


def requested_by(alloc: ComputeAllocation) -> tuple[dict[str, Any], dict[str, Any], str]:
    """``(actor, chain, reason)`` of the wake waiting on the row."""
    request = alloc.wake_request_json or {}
    return (
        dict(request.get("actor") or {}),
        dict(request.get("chain") or {}),
        str(request.get("reason") or "wake"),
    )


async def complete_wake(
    db: AsyncSession, allocation_id: UUID, provider: NodeProvider, *, now: datetime
) -> bool:
    """The reconcile's turn at a wake the provider refused: start the machine
    through :func:`wake_allocation`, as whoever asked, or leave the request
    for the next pass while the provider still refuses. Returns whether it
    started. Commits.

    The start is simply tried again rather than predicted from the provider's
    status word: every provider names its stopping state differently (EC2
    ``stopping``, a RunPod pod still ``RUNNING`` after its stop), and the
    provider's own refusal is the one answer that is always right."""
    alloc = await db.get(ComputeAllocation, allocation_id, populate_existing=True)
    if alloc is None or alloc.state != ASLEEP or alloc.wake_requested_at is None:
        await db.commit()
        return False
    if wake_expired(alloc, now=now):
        locked = await _locked(db, allocation_id)
        if locked is not None and locked.wake_requested_at is not None:
            drop_wake(locked, why="the provider did not allow the start within the bound")
        await db.commit()
        return False
    actor, chain, reason = requested_by(alloc)
    result = await wake_allocation(
        db, allocation_id, provider, actor=actor, chain=chain, reason=reason, now=now, retry=True
    )
    if result.refused is not None:
        log.info(
            "compute.wake.not_yet", allocation_id=str(allocation_id), error=str(result.refused)
        )
    elif result.started:
        log.info("compute.wake.completed", allocation_id=str(allocation_id))
    return result.started


__all__ = [
    "WAKE_REQUEST_TTL",
    "WakeResult",
    "complete_wake",
    "drop_wake",
    "remember_wake",
    "requested_by",
    "wake_allocation",
    "wake_expired",
]
