"""One meter pass over the boxes that run, each beating first.

Its own module: it runs a fleet-wide pass, and a module that imports a helper
counts as running every pass the helper's module calls. Only the modules that
meter (all in the ``compute-fleet`` lane) import this one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alkera_core.compute.meter import MeterSummary, meter_and_cutoff
from alkera_core.compute.provider import RUNNING
from alkera_core.models.compute import ComputeAllocation
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider


async def meter_live(
    session: AsyncSession, *, provider: FakeProvider, now: datetime | None = None, **kwargs: Any
) -> MeterSummary:
    """One meter pass over boxes that are up and heard from.

    A box beats every few seconds for as long as it runs, and the meter bills
    a box only while it is heard from. So every allocation whose pod
    ``provider`` reports running beats at the pass's moment first; a pod the
    provider reports gone or does not know, or one it names ``silent``, gets
    no beat."""
    at = now or datetime.now(UTC)
    running_pods = [
        pod
        for pod, status in provider.status_map.items()
        if status.phase == RUNNING and pod not in provider.silent
    ]
    if running_pods:
        await session.execute(
            update(ComputeAllocation)
            .where(ComputeAllocation.provider_machine_id.in_(running_pods))
            .values(last_heartbeat_at=at)
        )
        await session.commit()
    return await meter_and_cutoff(session, provider=provider, now=now, **kwargs)
