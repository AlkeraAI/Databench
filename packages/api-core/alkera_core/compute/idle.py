"""When an org machine has idled long enough to stop on its own.

Pure: plain rows and a clock in, a decision out. The org-machine reconcile asks
:func:`is_idle` of each running machine with an idle window, and for an org's
pool machines :func:`pool_machines_to_stop` keeps the org's ``min_awake_pool``
awake before any of them is stopped.

A machine is idle when all of these hold:

- it is ``ready`` (not starting, not already draining toward a stop);
- it has an idle window: the one its org set (``idle_stop_minutes``;
  ``None`` is never), or, for an assigned machine nobody may use any more
  (its audience is empty), the offering's default and else
  :data:`UNASSIGNED_IDLE_MINUTES`. A machine no person can reach would
  otherwise run, and bill, for ever;
- it has been ready for at least the window, so a machine just started or
  woken gets its full window before anyone can call it idle;
- either it serves no chat at all, or nothing on it has done any work for the
  window. The last work is the later of what its box reported
  (``last_activity_at``) and the moment it became ready: a box that has not
  worked since a wake is measured from the wake, not from before the sleep,
  and a box too old to report activity is measured from when it came up.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from alkera_core.models.compute import READY, ComputeAllocation
from alkera_core.models.org_machines import USE_ASSIGNED, OrgMachine

#: How long an assigned machine with nobody left in its audience runs idle
#: before it stops, when neither its org nor its offering names a window.
UNASSIGNED_IDLE_MINUTES = 60


def _became_ready(alloc: ComputeAllocation) -> datetime | None:
    return alloc.state_changed_at or alloc.ready_at


def last_work(alloc: ComputeAllocation) -> datetime | None:
    """The later of the box's last reported work and when it became ready."""
    ready = _became_ready(alloc)
    reported = alloc.last_activity_at
    if ready is None:
        return reported
    if reported is None:
        return ready
    return max(ready, reported)


def idle_window_minutes(
    om: OrgMachine, *, audience_empty: bool = False, offering_default: int | None = None
) -> int | None:
    """The idle window ``om`` stops after, in minutes; ``None`` for never.

    The org's own setting wins. With none, an assigned machine whose audience
    is empty (its last person left, its last team was deleted) still stops:
    after the offering's default window, else :data:`UNASSIGNED_IDLE_MINUTES`.
    A pool machine's audience is the whole org, so it is never unassigned."""
    if om.idle_stop_minutes is not None and om.idle_stop_minutes > 0:
        return om.idle_stop_minutes
    if audience_empty and om.use_mode == USE_ASSIGNED:
        if offering_default is not None and offering_default > 0:
            return offering_default
        return UNASSIGNED_IDLE_MINUTES
    return None


def is_idle(
    om: OrgMachine,
    alloc: ComputeAllocation | None,
    now: datetime,
    *,
    audience_empty: bool = False,
    offering_default: int | None = None,
) -> bool:
    """Whether ``om``'s machine should stop for idling now (module docstring)."""
    if alloc is None or alloc.state != READY or om.deleted_at is not None:
        return False
    minutes = idle_window_minutes(
        om, audience_empty=audience_empty, offering_default=offering_default
    )
    if minutes is None:
        return False
    window = timedelta(minutes=minutes)
    ready = _became_ready(alloc)
    if ready is None or now - ready < window:
        return False
    if alloc.chats_served == 0:
        return True
    worked = last_work(alloc)
    return worked is not None and now - worked >= window


@dataclass(frozen=True)
class PoolMachine:
    """One of an org's pool machines as the keep-awake rule sees it."""

    org_machine: OrgMachine
    allocation: ComputeAllocation | None
    idle: bool


def pool_machines_to_stop(machines: Sequence[PoolMachine], *, min_awake: int) -> list[OrgMachine]:
    """The idle pool machines to stop, keeping ``min_awake`` running.

    Only running (``ready``) machines count toward the ones kept awake. The
    least recently active idle machines are stopped first, and never so many
    that fewer than ``min_awake`` stay running."""
    running = [m for m in machines if m.allocation is not None and m.allocation.state == READY]
    may_stop = max(0, len(running) - max(0, min_awake))
    idle = [m for m in running if m.idle]

    def recency(machine: PoolMachine) -> float:
        worked = last_work(machine.allocation) if machine.allocation is not None else None
        return worked.timestamp() if worked is not None else float("-inf")

    idle.sort(key=recency)
    return [m.org_machine for m in idle[:may_stop]]


__all__ = [
    "UNASSIGNED_IDLE_MINUTES",
    "PoolMachine",
    "idle_window_minutes",
    "is_idle",
    "last_work",
    "pool_machines_to_stop",
]
