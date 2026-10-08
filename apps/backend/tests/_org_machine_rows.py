"""Org machines written straight into the database, for the provider tests.

An offering of a machine type, the org machine bought from it, and the
allocation backing it, tied together the way the schema requires: the
allocation carries the org machine and the org as its tenant, the org machine
points back at it. The allocation is filed under the org itself, so a machine
credential minted for it speaks in that org.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.compute.machines import WORKSPACE
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    PROVISIONED,
    READY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import OrgMachine
from sqlalchemy.ext.asyncio import AsyncSession


async def make_offering(
    session: AsyncSession, machine_type: ComputeMachineType, **overrides: Any
) -> ComputeOffering:
    values: dict[str, Any] = {
        "machine_type_id": machine_type.id,
        "name": f"{machine_type.display_name} offering",
        "pricing_mode": "pass_through",
        "markup_bps": 0,
        "storage_gb_default": 50,
        "storage_gb_max": 500,
        "audience": "all",
    }
    values.update(overrides)
    offering = ComputeOffering(**values)
    session.add(offering)
    await session.flush()
    return offering


async def make_org_machine(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    machine_type: ComputeMachineType,
    name: str | None = None,
    state: str = READY,
    price_per_minute_nanos: int = 0,
    idle_stop_minutes: int | None = None,
    storage_gb: int = 50,
    desired_power: str = "on",
    use_mode: str = "assigned",
    acquisition: str = "purchased",
    with_allocation: bool = True,
    **alloc_overrides: Any,
) -> tuple[OrgMachine, ComputeAllocation | None]:
    """An org machine of ``machine_type`` in ``org_id`` and, unless told not
    to, the allocation backing it in ``state``. Commits."""
    offering = await make_offering(session, machine_type)
    om = OrgMachine(
        org_team_id=org_id,
        owner_team_id=org_id,
        offering_id=offering.id,
        name=name or f"Machine {uuid.uuid4().hex[:6]}",
        acquisition=acquisition,
        free_until=datetime(2099, 1, 1, tzinfo=UTC) if acquisition == "granted" else None,
        use_mode=use_mode,
        storage_gb=storage_gb,
        idle_stop_minutes=idle_stop_minutes,
        desired_power=desired_power,
    )
    session.add(om)
    await session.flush()
    alloc: ComputeAllocation | None = None
    if with_allocation:
        alloc = ComputeAllocation(
            user_id=user_id,
            org_team_id=org_id,
            machine_type_id=machine_type.id,
            lifecycle=WORKSPACE,
            origin=PROVISIONED,
            tenancy=DEDICATED_TENANCY,
            tenant_org_id=org_id,
            org_machine_id=om.id,
            state=state,
            storage_gb=storage_gb,
            price_per_minute_nanos=price_per_minute_nanos,
            true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
            name=om.name,
            **alloc_overrides,
        )
        session.add(alloc)
        await session.flush()
        om.current_allocation_id = alloc.id
    await session.commit()
    return om, alloc
