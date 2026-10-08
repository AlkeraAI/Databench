"""A machine's provider stop and start, asked after its rows commit, never
under them.

A stop or start at the provider takes seconds (a RunPod pod) to minutes (a
container on a busy Docker host). Asked while holding the org machine's, the
allocation's and every bound chat's row lock, it would freeze everything that
touches the machine for that long: chat messages, a Replace, the box's
heartbeats and the reconcile passes would all time out on the lock.

So the sleep (or the wake's intent) commits first, and the provider is asked
afterwards, in a transaction that holds only this machine's power claim
(:func:`power_claim`, taken through
:func:`alkera_core.db.locking.claimed_io`): a transaction-scoped advisory lock
keyed by the allocation. A stop and a start both take it, so they cannot race:

* a wake that claimed first is starting the machine, so the stop is not
  asked (the row it re-reads is no longer ``asleep``, or the claim is taken);
* a stop that claimed first runs to the end, and a wake meeting it is refused
  as a start the provider is still stopping for (:class:`StopInFlightError`),
  which every wake keeps on the row for the reconcile to complete.

Nobody waits on the claim: both sides only try it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.nodes import NodeProvider
from alkera_core.compute.provider import TRANSIENT_FAILURE, ComputeProviderError
from alkera_core.db.locking import AdvisoryKey, advisory_key, claimed_io
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    ASLEEP,
    PROVISIONED,
    ComputeAllocation,
)

log = get_logger(__name__)


class StopInFlightError(ComputeProviderError):
    """A start asked while the machine's stop is still with the provider."""

    def __init__(self) -> None:
        super().__init__("the machine is still stopping", kind=TRANSIENT_FAILURE)


def power_claim(allocation_id: UUID) -> AdvisoryKey:
    """The claim a stop and a start of ``allocation_id``'s machine both take."""
    return advisory_key("compute-power", allocation_id)


async def stop_after_sleep(db: AsyncSession, allocation_id: UUID, provider: NodeProvider) -> bool:
    """Ask the provider to stop a machine whose sleep has committed, holding
    only its power claim. Not asked when a wake holds the claim or the row is
    no longer ``asleep``. Best effort: the node reconcile stops a machine that
    stayed running. Commits; returns whether the stop was asked."""
    async with claimed_io(db, power_claim(allocation_id)) as claimed:
        if not claimed:
            log.info("compute.sleep.stop_skipped", allocation_id=str(allocation_id), why="waking")
            return False
        row = (
            await db.execute(
                select(
                    ComputeAllocation.state,
                    ComputeAllocation.provider_machine_id,
                    ComputeAllocation.origin,
                ).where(ComputeAllocation.id == allocation_id)
            )
        ).one_or_none()
        if row is None or row.state != ASLEEP or not row.provider_machine_id:
            return False
        if row.origin != PROVISIONED:
            return False
        try:
            await provider.stop(row.provider_machine_id)
        except ComputeProviderError as exc:
            log.warning(
                "compute.sleep.stop_deferred", allocation_id=str(allocation_id), error=str(exc)
            )
            return False
        return True


__all__ = ["StopInFlightError", "power_claim", "stop_after_sleep"]
