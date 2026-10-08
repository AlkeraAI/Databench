"""The one door a provisioned machine's ``state`` goes through.

:data:`LEGAL_EDGES` is the machine lifecycle as data: every state names the
states it may move to, and :func:`transition` refuses anything else. Each move
stamps ``state_changed_at`` and appends a ``compute_allocation_events`` row in
the caller's session, so the edge and the history commit (or roll back)
together. A self-edge is not a transition and is refused like any other, so a
caller cannot write a history row that says nothing happened.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.models.compute import (
    ASLEEP,
    BOOTSTRAPPING,
    COMPUTE_ALLOCATION_STATES,
    DRAINING,
    FAILED,
    LOST,
    PENDING,
    PROVISIONED,
    PROVISIONING,
    READY,
    RELEASED,
    RELEASING,
    ComputeAllocation,
    ComputeAllocationEvent,
)

LEGAL_EDGES: Mapping[str, frozenset[str]] = {
    PENDING: frozenset({PROVISIONING, FAILED}),
    PROVISIONING: frozenset({BOOTSTRAPPING, READY, FAILED, RELEASING}),
    BOOTSTRAPPING: frozenset({READY, FAILED, RELEASING}),
    READY: frozenset({DRAINING, ASLEEP, RELEASING, LOST, FAILED}),
    # A drain ends in a sleep when the machine is being stopped (its disk kept)
    # rather than released: an org machine's credit, cap, idle or user stop.
    DRAINING: frozenset({READY, ASLEEP, RELEASING, LOST, FAILED}),
    # Asleep is stopped-but-kept: wake back to ready, off to releasing, or the
    # provider lost / failed the stopped machine out from under us.
    ASLEEP: frozenset({READY, RELEASING, LOST, FAILED}),
    RELEASING: frozenset({RELEASED, FAILED}),
    LOST: frozenset({RELEASED}),
    RELEASED: frozenset(),
    # A failed machine is already off the plane; an admin's terminate ends
    # whatever the provider may still hold for it and records the release.
    FAILED: frozenset({RELEASED}),
}
"""from-state -> the states it may move to. Terminal states move nowhere."""


class IllegalTransitionError(ValueError):
    """A move the lifecycle does not allow."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"a machine cannot go from {from_state!r} to {to_state!r}")


REVIVABLE_STATES: frozenset[str] = frozenset({RELEASING, RELEASED, FAILED})
"""The states a REGISTERED machine's row may be put back to ``ready`` from."""


def is_legal(from_state: str, to_state: str) -> bool:
    return to_state in LEGAL_EDGES.get(from_state, frozenset())


def transition(
    db: AsyncSession,
    alloc: ComputeAllocation,
    to: str,
    *,
    reason: str = "",
    actor: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ComputeAllocationEvent:
    """Move ``alloc`` to ``to`` and record the edge. Raises
    :class:`IllegalTransitionError` (and changes nothing) for an edge
    :data:`LEGAL_EDGES` does not name. Adds to the session; the caller commits."""
    if to not in COMPUTE_ALLOCATION_STATES or not is_legal(alloc.state, to):
        raise IllegalTransitionError(alloc.state, to)
    moment = now or datetime.now(UTC)
    if alloc.state_changed_at is not None and moment <= alloc.state_changed_at:
        # Two edges in one pass (ready -> lost -> released) share a clock
        # reading, and the worker's clock may trail the backend's. The history
        # is read in ``at`` order, so each edge lands strictly after the one
        # before it or the admin page shows a machine released before it was lost.
        moment = alloc.state_changed_at + timedelta(microseconds=1)
    event = ComputeAllocationEvent(
        allocation_id=alloc.id,
        at=moment,
        from_state=alloc.state,
        to_state=to,
        reason=reason,
        actor=dict(actor or {}),
    )
    alloc.state = to
    alloc.state_changed_at = moment
    if to == READY and alloc.ready_at is None:
        alloc.ready_at = moment
    if to in (RELEASED, FAILED):
        alloc.released_at = moment
    db.add(event)
    return event


def revive(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Put a finished machine the plane did not start back to ``ready``.

    A box a daemon REGISTERED is the daemon's to bring back: the row it was
    reaped on is the row it registers onto again. That is the one move out of
    a finished state, so it is its own door rather than an edge in
    :data:`LEGAL_EDGES`, and it is refused for a machine the plane
    PROVISIONED — that one is finished for good, and a box still holding its
    credential must not bring the row back. Records the edge like
    :func:`transition`."""
    if alloc.origin == PROVISIONED or alloc.state not in REVIVABLE_STATES:
        raise IllegalTransitionError(alloc.state, READY)
    moment = now or datetime.now(UTC)
    if alloc.state_changed_at is not None and moment <= alloc.state_changed_at:
        moment = alloc.state_changed_at + timedelta(microseconds=1)
    db.add(
        ComputeAllocationEvent(
            allocation_id=alloc.id,
            at=moment,
            from_state=alloc.state,
            to_state=READY,
            reason=reason,
            actor=dict(actor or {}),
        )
    )
    alloc.state = READY
    alloc.state_changed_at = moment
    alloc.ready_at = moment
    alloc.released_at = None
    return alloc


__all__ = [
    "LEGAL_EDGES",
    "REVIVABLE_STATES",
    "IllegalTransitionError",
    "is_legal",
    "revive",
    "transition",
]
