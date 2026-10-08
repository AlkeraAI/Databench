"""The activity behind the identity security log's retention prune: a thin
wrapper around the core in ``worker.tasks.identity_security``, heartbeating so
a worker that dies mid-backlog is noticed in minutes."""

from __future__ import annotations

from alkera_core.logging import get_logger
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.activities._sweep import clock
from worker.tasks.identity_security import _prune_identity_security_events
from worker.temporal.heartbeat import heartbeating

log = get_logger(__name__)


@activity.defn(name=WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value)
async def prune_identity_security_events(input: SweepInput | None = None) -> int:
    """Daily: delete identity security events older than the retention window.
    Returns how many were deleted."""
    async with heartbeating():
        removed = await _prune_identity_security_events(clock(input))
    log.info("auth.prune_identity_security_events", removed=removed)
    return removed
