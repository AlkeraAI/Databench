"""Putting a machine to sleep: stopped at its provider with its disk kept.

:func:`sleep_allocation` is the one sleep. A platform admin's sleep of a box
(``backend.services.compute.provisioning.sleep``) and the org-machine reconcile
stopping an org machine (:func:`sleep_org_allocation`) both go through it, so
each writes the same history edge, ends the chats the same way and stops the
provider machine under the same lock.

The order matters and is the same for every caller:

1. the row is locked and re-read, and refused unless it is in a state the
   caller may sleep from (an admin sleeps a ``ready`` box; the reconcile sleeps
   a ``draining`` org machine whose drain is over);
2. the row moves to ``asleep``;
3. the caller's ``rebind`` (when it has one) offers the chats to other boxes;
4. every chat still bound is restated ``asleep`` and ended with the caller's
   reason, through the one chat-ending transition;
5. the machine's own frame says ``asleep``.

Nothing here commits and nothing here asks the provider: the caller's
transaction carries the edge, the chat endings and the frames together, and
after it commits the caller asks the provider to stop under the machine's
power claim (:func:`~alkera_core.compute.power_lock.stop_after_sleep`), which
every wake takes too. A stop takes seconds to minutes at a provider, and every
chat request and heartbeat of the machine touches the rows a sleep holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.events import announce_machine
from alkera_core.compute.handoff import restate_bound_chats
from alkera_core.compute.machines import ASLEEP as ASLEEP_STATUS
from alkera_core.compute.node_reconcile import revoke_node_credentials
from alkera_core.compute.nodes import NodeProvider
from alkera_core.compute.provider import GONE, ComputeProviderError
from alkera_core.compute.transitions import transition
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import actor_system
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    ASLEEP,
    COMPUTE_TERMINAL_STATES,
    DRAIN_CAP,
    DRAIN_CREDITS,
    DRAINING,
    FAILED,
    PENDING,
    PROVISIONED,
    READY,
    RELEASED,
    RELEASING,
    TERMINATED_ORG_DELETED,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.objects import chat_end

log = get_logger(__name__)

#: The actor a sleep the plane decided on writes on the machine's history.
RECONCILE_ACTOR: dict[str, Any] = {"kind": "system", "email": None, "name": "reconcile"}

Rebind = Callable[[ComputeAllocation], Awaitable[int]]


class NotSleepableError(Exception):
    """The machine is not in a state the caller may sleep it from."""

    def __init__(self, state: str) -> None:
        super().__init__(f"A {state} machine cannot be slept")
        self.state = state


async def sleep_allocation(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    actor: Mapping[str, Any],
    chain: Mapping[str, Any],
    end_reason: chat_end.ChatEndReason,
    reason: str = "sleep",
    from_states: tuple[str, ...] = (READY,),
    rebind: Rebind | None = None,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Sleep ``alloc``: the steps in the module docstring. Returns the row as
    re-read under its lock. Raises :class:`NotSleepableError` (and changes
    nothing) for a row not in ``from_states``. Does not commit; the caller
    asks the provider to stop after its commit
    (:func:`~alkera_core.compute.power_lock.stop_after_sleep`)."""
    moment = now or datetime.now(UTC)
    locked = (
        await lock_rows(
            db,
            LockRank.ALLOCATION,
            select(ComputeAllocation)
            .where(ComputeAllocation.id == alloc.id)
            .execution_options(populate_existing=True),
        )
    ).scalar_one()
    if locked.state not in from_states:
        raise NotSleepableError(locked.state)
    transition(db, locked, ASLEEP, reason=reason, actor=dict(actor), now=moment)
    # A drain that ended in this sleep is over; the row carries no drain while
    # it sleeps, so a wake starts it clean.
    locked.drain_requested_at = None
    locked.drain_reason = ""
    locked.drain_kind = None
    locked.drain_deadline_at = None
    locked.auto_terminate = False
    if rebind is not None:
        await rebind(locked)
    await restate_bound_chats(db, machine_id=locked.id, status="asleep", actor=chain)
    # What nothing could take ends here, through the one transition: each chat
    # reads asleep and its folder's lease goes with the box that held it.
    await chat_end.end_chats(
        db,
        await chat_end.live_chats_bound_to(db, locked.id),
        end_reason,
        actor=chain,
        # Each chat was announced by the restate above; one frame per change.
        announce=False,
    )
    await announce_machine(db, locked, status=ASLEEP_STATUS, reason=None, actor=chain)
    return locked


def end_reason_for_drain(drain_kind: str | None) -> chat_end.ChatEndReason:
    """How the chats of an org machine stopped after a drain of ``drain_kind``
    end: out of credit for a funding or a cap stop, stopped otherwise."""
    if drain_kind in (DRAIN_CREDITS, DRAIN_CAP):
        return chat_end.ChatEndReason.CREDITS_EXHAUSTED
    return chat_end.ChatEndReason.MACHINE_STOPPED


async def sleep_org_allocation(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Sleep an org machine's allocation whose drain is over (or a ready one
    asked to stop). The chats still bound end with the reason the drain names
    (:func:`end_reason_for_drain`). The caller asks the provider to stop after
    its commit (:func:`~alkera_core.compute.power_lock.stop_after_sleep`).
    Does not commit."""
    return await sleep_allocation(
        db,
        alloc,
        actor=RECONCILE_ACTOR,
        chain=actor_system("compute"),
        end_reason=end_reason_for_drain(alloc.drain_kind),
        reason=f"stopped ({alloc.drain_kind or 'user'})",
        from_states=(DRAINING, READY),
        now=now,
    )


class OrgMachinesNotReleasedError(Exception):
    """Some of a departing org's machines could not be terminated: the org's
    rows must not be purged, or nothing would be left to find them by."""

    def __init__(self, machine_ids: Sequence[str]) -> None:
        super().__init__(
            "the provider did not confirm these machines gone: " + ", ".join(machine_ids)
        )
        self.machine_ids = list(machine_ids)


ProviderLookup = Callable[[str], NodeProvider]


async def release_for_org_deletion(
    db: AsyncSession,
    org_id: UUID,
    *,
    providers: ProviderLookup,
    now: datetime | None = None,
) -> list[UUID]:
    """Terminate, now, every machine that holds ``org_id``'s data, before the
    org's rows are purged.

    An org's deletion removes its rows in one transaction, cascading through
    its org machines and their allocations; a reconcile that would have
    released them later finds nothing to release, and the provider machines
    (and their disks, which hold the org's files) would run on unseen. So the
    deletion calls this first: every allocation whose tenant is the org and
    that has not ended is terminated at its provider and confirmed gone, its
    node secret deleted there, its credentials revoked, and the row ended
    ``org_deleted`` (in the caller's transaction, which the purge commits or
    rolls back).

    The providers are asked before any allocation row is locked, and only the
    rows of the machines confirmed gone are locked, at the end, to end them:
    a terminate and the describe that confirms it take seconds per machine,
    and the boxes' own heartbeats touch these rows meanwhile.

    Raises :class:`OrgMachinesNotReleasedError` naming every machine the
    provider did not confirm gone, after trying them all: the caller must not
    purge, so a later attempt still finds them. Returns the allocations it
    ended. Never commits."""
    moment = now or datetime.now(UTC)
    candidates = (
        await db.execute(
            select(
                ComputeAllocation.id,
                ComputeAllocation.provider_machine_id,
                ComputeAllocation.origin,
                ComputeMachineType.provider,
            )
            .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
            .where(
                ComputeAllocation.tenant_org_id == org_id,
                ComputeAllocation.state.not_in(COMPUTE_TERMINAL_STATES),
            )
            .order_by(ComputeAllocation.created_at)
        )
    ).all()
    gone: list[UUID] = []
    refused: list[str] = []
    for allocation_id, machine_id, origin, kind in candidates:
        provider = providers(kind)
        if machine_id and origin == PROVISIONED:
            try:
                await provider.terminate(machine_id)
                confirmed = (await provider.describe(machine_id)).phase == GONE
            except ComputeProviderError as exc:
                log.warning(
                    "compute.org_deleted.terminate_refused",
                    allocation_id=str(allocation_id),
                    error=str(exc),
                )
                confirmed = False
            if not confirmed:
                refused.append(machine_id)
                continue
        try:
            await provider.delete_credential(allocation_id)
        except ComputeProviderError as exc:
            log.warning(
                "compute.org_deleted.secret_leftover",
                allocation_id=str(allocation_id),
                error=str(exc),
            )
        gone.append(allocation_id)
    if refused:
        raise OrgMachinesNotReleasedError(refused)
    ended: list[UUID] = []
    rows = (
        await lock_rows(
            db,
            LockRank.ALLOCATION,
            select(ComputeAllocation)
            .where(
                ComputeAllocation.id.in_(gone),
                ComputeAllocation.state.not_in(COMPUTE_TERMINAL_STATES),
            )
            .order_by(ComputeAllocation.created_at)
            .execution_options(populate_existing=True),
        )
    ).scalars()
    for alloc in rows:
        alloc.terminated_reason = TERMINATED_ORG_DELETED
        alloc.wake_requested_at = None
        alloc.wake_request_json = None
        if alloc.state == PENDING:
            transition(
                db, alloc, FAILED, reason="the org was deleted", actor=RECONCILE_ACTOR, now=moment
            )
        else:
            if alloc.state != RELEASING:
                transition(
                    db,
                    alloc,
                    RELEASING,
                    reason="the org was deleted",
                    actor=RECONCILE_ACTOR,
                    now=moment,
                )
            transition(
                db,
                alloc,
                RELEASED,
                reason="the provider released the machine",
                actor=RECONCILE_ACTOR,
                now=moment,
            )
        await revoke_node_credentials(db, alloc, now=moment)
        ended.append(alloc.id)
        log.info("compute.org_deleted.released", allocation_id=str(alloc.id), org_id=str(org_id))
    await db.flush()
    return ended


__all__ = [
    "RECONCILE_ACTOR",
    "NotSleepableError",
    "OrgMachinesNotReleasedError",
    "ProviderLookup",
    "Rebind",
    "end_reason_for_drain",
    "release_for_org_deletion",
    "sleep_allocation",
    "sleep_org_allocation",
]
