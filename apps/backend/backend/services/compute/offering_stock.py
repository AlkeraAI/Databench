"""The stock verdict of each offering a reader is shown, with the provider's
live answer for its size: one call per provider, through the per-process
availability cache, so a buy list re-read every few seconds asks a provider
at most once a minute.

The verdict itself is :func:`alkera_core.compute.stock.offer_verdict`'s; this
module only fetches the live answer it reads. A purchase asks with
``fresh=True``: it is the one moment a stale "in stock" costs a buyer a
machine that waits for hardware.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alkera_core.compute import availability as availability_core
from alkera_core.compute.availability import Availability, SizeQuery
from alkera_core.compute.stock import Stock, offer_verdict
from alkera_core.config import settings
from alkera_core.models.compute import ComputeMachineType
from alkera_core.schemas.org_machines import StockRead

from backend.services.compute import provisioning


async def live_answers(
    machine_types: Sequence[ComputeMachineType], *, fresh: bool = False
) -> dict[UUID, Availability]:
    """The provider's answer for each type's size, by type id. A provider this
    deployment cannot act through, or one that did not answer, gives none."""
    out: dict[UUID, Availability] = {}
    for kind in sorted({mt.provider for mt in machine_types}):
        provider = provisioning.make_node_provider(kind, settings)
        if not provider.configured():
            continue
        of_kind = [mt for mt in machine_types if mt.provider == kind]
        sizes = [SizeQuery(code=mt.provider_type_id, vcpu=mt.vcpu) for mt in of_kind]
        cache = availability_core.AvailabilityCache(ttl=0) if fresh else availability_core.CACHE
        answers = await cache.lookup(kind, sizes, provider.availability)
        for mt in of_kind:
            if (answer := answers.get(mt.provider_type_id)) is not None:
                out[mt.id] = answer
    return out


def verdict(
    machine_type: ComputeMachineType, live: dict[UUID, Availability] | None = None
) -> Stock:
    return offer_verdict(
        machine_type,
        node_api_url=settings.node_api_url,
        live=(live or {}).get(machine_type.id),
    )


def stock_read(stock: Stock) -> StockRead:
    return StockRead(state=stock.state, can_buy=stock.can_buy, reason=stock.reason)


__all__ = ["live_answers", "stock_read", "verdict"]
