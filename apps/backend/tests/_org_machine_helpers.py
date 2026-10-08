"""Factories for org machines in the tests: an offering, an org machine with
an audience, and the provider machine behind it in any state a person can see
(running, stopped, starting, waiting for hardware, couldn't start, deleted).

The rows are written the way the plane leaves them: the allocation's tenant is
set before the org machine points at it (the composite foreign keys require
the pairing), a running or stopped machine is held by a live machine
credential, and a pending one has none yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from alkera_core.models import ComputeAllocation, ComputeMachineType, Team, WorkspaceObject
from alkera_core.models.compute import DEDICATED_TENANCY
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import OrgComputeSettings, OrgMachine, OrgMachineAudience
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type

MachineLook = Literal["running", "stale", "stopped", "starting", "waiting", "failed", "deleted"]

#: An audience grant as a test writes it: ``("org",)``, ``("team", id)``,
#: ``("user", id)``.
Grant = tuple[str] | tuple[str, UUID]


async def make_offering(
    session: AsyncSession,
    *,
    machine_type: ComputeMachineType | None = None,
    audience: str = "all",
    fixed_rate: int = 0,
    purchasable: bool = True,
    storage_gb_default: int = 50,
    storage_gb_max: int = 500,
    retired: bool = False,
) -> ComputeOffering:
    mt = machine_type or await make_machine_type(session, provider="runpod", compute_class="gpu")
    offering = ComputeOffering(
        machine_type_id=mt.id,
        name=f"Offering {uuid4().hex[:6]}",
        pricing_mode="fixed",
        fixed_rate_per_minute_nanos=fixed_rate,
        markup_bps=0,
        storage_gb_default=storage_gb_default,
        storage_gb_max=storage_gb_max,
        storage_rate_per_gb_month_nanos=0,
        audience=audience,
        purchasable=purchasable,
        retired_at=datetime.now(UTC) if retired else None,
    )
    session.add(offering)
    await session.commit()
    return offering


async def make_org_machine(
    session: AsyncSession,
    *,
    org_id: UUID,
    operator_id: UUID,
    look: MachineLook = "running",
    use_mode: str = "assigned",
    audience: Sequence[Grant] = (("org",),),
    owner_team_id: UUID | None = None,
    name: str | None = None,
    chats_served: int = 0,
    offering: ComputeOffering | None = None,
) -> tuple[OrgMachine, ComputeAllocation]:
    """An org machine of ``org_id`` and the provider machine behind it.
    ``operator_id`` is the person the allocation is recorded against."""
    offering = offering or await make_offering(session)
    label = name or f"Machine {uuid4().hex[:6]}"
    machine = OrgMachine(
        org_team_id=org_id,
        owner_team_id=owner_team_id or org_id,
        offering_id=offering.id,
        name=label,
        acquisition="purchased",
        use_mode=use_mode,
        storage_gb=50,
        desired_power="off" if look == "stopped" else "on",
        stop_reason="idle" if look == "stopped" else "",
        deleted_at=datetime.now(UTC) if look == "deleted" else None,
    )
    session.add(machine)
    await session.flush()
    now = datetime.now(UTC)
    state = {
        "running": "ready",
        "stale": "ready",
        "stopped": "asleep",
        "starting": "pending",
        "waiting": "failed",
        "failed": "failed",
        "deleted": "ready",
    }[look]
    alloc = ComputeAllocation(
        user_id=operator_id,
        org_team_id=org_id,
        machine_type_id=offering.machine_type_id,
        lifecycle="workspace",
        origin="provisioned",
        name=label,
        tenancy=DEDICATED_TENANCY,
        sandbox="gvisor",
        capacity=6,
        chats_served=chats_served,
        state=state,
        provider_machine_id=None if look == "starting" else f"pod-{uuid4().hex[:8]}",
        created_at=now,
        ready_at=now if state == "ready" else None,
        state_changed_at=now,
        last_metered_at=now,
        last_heartbeat_at=(
            now
            if look in ("running", "deleted")
            else now - timedelta(hours=1)
            if look == "stale"
            else None
        ),
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=0,
        tenant_org_id=org_id,
        org_machine_id=machine.id,
        failure_kind="capacity" if look == "waiting" else "",
        terminated_reason=(
            "provider_capacity"
            if look == "waiting"
            else "boot_failed"
            if look == "failed"
            else None
        ),
    )
    session.add(alloc)
    await session.flush()
    machine.current_allocation_id = alloc.id
    for grant in audience:
        session.add(
            OrgMachineAudience(
                org_team_id=org_id,
                org_machine_id=machine.id,
                grantee_kind=grant[0],
                team_id=grant[1] if grant[0] == "team" else None,
                user_id=grant[1] if grant[0] == "user" else None,
            )
        )
    await session.commit()
    if look in ("running", "stale", "stopped", "deleted"):
        await hold_with_credential(session, alloc)
    return machine, alloc


async def backfilled_org_machine(
    session: AsyncSession, *, org_id: UUID, box: ComputeAllocation, fallback: bool = False
) -> OrgMachine:
    """What the migration made of a dedicated-box assignment: a granted org
    machine in the org pool over ``box``, a free offering of its machine type
    listed for the org, the org's fallback setting, and the org on the plan
    the org pool needs."""
    from alkera_core.compute.billing_port import compute_billing
    from tests.conftest import make_org_enterprise

    mt = await session.get(ComputeMachineType, box.machine_type_id)
    offering = await make_offering(session, machine_type=mt, audience="listed")
    machine = OrgMachine(
        org_team_id=org_id,
        owner_team_id=org_id,
        offering_id=offering.id,
        name=box.name or "Machine 1",
        acquisition="granted",
        free_until=datetime.now(UTC) + timedelta(days=365),
        use_mode="pool",
        storage_gb=max(box.storage_gb or 0, 1),
    )
    session.add(machine)
    await session.flush()
    box.tenant_org_id = org_id
    box.org_machine_id = machine.id
    await session.flush()
    machine.current_allocation_id = box.id
    await session.commit()
    await set_fallback(session, org_id=org_id, fallback=fallback)
    if not await compute_billing().holds_enterprise_plan(session, org_id):
        await make_org_enterprise(org_id)
    return machine


async def set_fallback(session: AsyncSession, *, org_id: UUID, fallback: bool) -> None:
    row = await session.get(OrgComputeSettings, org_id)
    if row is None:
        session.add(OrgComputeSettings(org_team_id=org_id, shared_pool_fallback=fallback))
    else:
        row.shared_pool_fallback = fallback
    await session.commit()


async def make_team(session: AsyncSession, *, org_id: UUID, name: str | None = None) -> Team:
    from backend.services.org import teams as team_service

    team = await team_service.create_subteam(
        session, org_team_id=org_id, name=name or f"Team {uuid4().hex[:6]}"
    )
    await session.commit()
    return team


async def pin_workspace(session: AsyncSession, workspace_id: UUID, pin: UUID | str | None) -> None:
    """Write a workspace's pin the way the move service leaves it."""
    workspace = await session.get(WorkspaceObject, workspace_id)
    assert workspace is not None
    spec = dict(workspace.spec or {})
    spec["machine_pin"] = str(pin) if pin is not None else None
    workspace.spec = spec
    await session.commit()


__all__ = [
    "Grant",
    "MachineLook",
    "backfilled_org_machine",
    "make_offering",
    "make_org_machine",
    "make_team",
    "pin_workspace",
    "set_fallback",
]
