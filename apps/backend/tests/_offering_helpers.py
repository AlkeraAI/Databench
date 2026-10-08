"""Build offerings for a test and take them away again.

Offerings are a platform table and hold their machine type by RESTRICT, and the
org machines bought from them hold the offering the same way, so a test that
leaves one behind breaks every later module on its worker that clears the
catalog. :class:`OfferingFactory` records what it made and
:func:`delete_offerings` removes it, machines first.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering, ComputeOfferingOrg
from alkera_core.models.org_machines import OrgMachine
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def delete_offerings(session: AsyncSession, offering_ids: Iterable[UUID] | None) -> None:
    """Remove the offerings (every one when ``offering_ids`` is None) and the
    org machines bought from them; their allocations go off the plane."""
    ids = None if offering_ids is None else list(offering_ids)
    if ids is not None and not ids:
        return
    machines = select(OrgMachine.id)
    if ids is not None:
        machines = machines.where(OrgMachine.offering_id.in_(ids))
    machine_ids = list((await session.execute(machines)).scalars())
    if machine_ids:
        await session.execute(
            update(ComputeAllocation)
            .where(ComputeAllocation.org_machine_id.in_(machine_ids))
            .values(org_machine_id=None, state="released")
        )
        await session.execute(delete(OrgMachine).where(OrgMachine.id.in_(machine_ids)))
    offerings = delete(ComputeOffering)
    if ids is not None:
        offerings = offerings.where(ComputeOffering.id.in_(ids))
    await session.execute(offerings)
    await session.commit()


async def delete_offerings_of_types(session: AsyncSession, provider_kinds: Sequence[str]) -> None:
    """Remove every offering of a machine type of these providers."""
    ids = list(
        (
            await session.execute(
                select(ComputeOffering.id)
                .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
                .where(ComputeMachineType.provider.in_(provider_kinds))
            )
        ).scalars()
    )
    await delete_offerings(session, ids)


class OfferingFactory:
    """Makes committed offerings and remembers them for teardown."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.made: list[UUID] = []

    async def make(
        self,
        machine_type: ComputeMachineType,
        *,
        org_ids: Sequence[UUID] = (),
        **over: Any,
    ) -> ComputeOffering:
        fields: dict[str, Any] = {
            "machine_type_id": machine_type.id,
            "name": f"Offering {len(self.made) + 1}",
            "pricing_mode": "pass_through",
            "markup_bps": 0,
            "storage_gb_default": 20,
            "storage_gb_max": 100,
            "storage_rate_per_gb_month_nanos": 0,
            "audience": "all",
            "purchasable": True,
            **over,
        }
        offering = ComputeOffering(**fields)
        self.session.add(offering)
        await self.session.flush()
        self.session.add_all(
            ComputeOfferingOrg(offering_id=offering.id, org_team_id=org) for org in org_ids
        )
        await self.session.commit()
        self.made.append(offering.id)
        return offering

    def remember(self, offering_id: UUID | str) -> None:
        self.made.append(UUID(str(offering_id)))

    async def close(self) -> None:
        await delete_offerings(self.session, self.made)


__all__ = ["OfferingFactory", "delete_offerings", "delete_offerings_of_types"]
