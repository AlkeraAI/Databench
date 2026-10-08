"""The ``compute_machine.changed`` frame — the one shape every machine
announcement takes, whoever emits it (the meter, the reachability sweep, the
register and heartbeat routes, a refused start).

The payload is exactly the two contract fields, ``status`` and ``reason``.
Nothing priced ever rides it: a tenant's event stream must never learn what a
machine cost us, and the cost-leak guard test walks this builder.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.models.compute import ComputeAllocation

SYSTEM_ACTOR = "compute"
"""The system actor a machine frame carries when no request principal emits it."""

MachineFrameStatus = str
"""``starting`` | ``ready`` | ``draining`` | ``restarting`` | ``unreachable`` |
``asleep`` | ``refused`` | ``none``."""


def machine_event_payload(*, status: MachineFrameStatus, reason: str | None) -> dict[str, Any]:
    """The frame's payload — the two contract fields, never a price."""
    return {"status": status, "reason": reason}


async def announce(
    db: AsyncSession,
    *,
    org_id: UUID,
    entity_id: str,
    status: MachineFrameStatus,
    reason: str | None,
    actor: Mapping[str, Any] | None = None,
) -> None:
    """Put one ``compute_machine.changed`` frame on the outbox in the caller's
    transaction. ``entity_id`` is the machine's allocation id, or the org id for a
    refusal that never produced a machine."""
    await emit(
        db,
        org_id=org_id,
        type=EventType.COMPUTE_MACHINE_CHANGED,
        entity=Entity.COMPUTE_MACHINE,
        entity_id=entity_id,
        payload=machine_event_payload(status=status, reason=reason),
        actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
    )


async def announce_machine(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    status: MachineFrameStatus,
    reason: str | None,
    actor: Mapping[str, Any] | None = None,
) -> None:
    """Announce ``alloc`` and remember what was announced, so the next tick emits
    only on a transition."""
    alloc.last_reported_status = status
    await announce(
        db,
        org_id=alloc.org_team_id,
        entity_id=str(alloc.id),
        status=status,
        reason=reason,
        actor=actor,
    )


__all__ = [
    "SYSTEM_ACTOR",
    "MachineFrameStatus",
    "announce",
    "announce_machine",
    "machine_event_payload",
]
