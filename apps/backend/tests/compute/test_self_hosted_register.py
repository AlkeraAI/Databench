"""A self-hosted box that registers itself is its org's machine.

The one-machine install's box registers through the same route as any
workspace machine. Under the ``self_hosted`` kind the registration also makes
it an org machine of the registering org (acquisition ``added``, the org's
pool, nothing charged), so the org's Machines page lists it and the admin
console names its org. A box of any other kind is left as it was.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.authz import agent_headers
from alkera_core.compute.provider import SELF_HOSTED
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import OrgMachine
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


async def _headers(org_admin: OrgWithAdmin) -> dict[str, str]:
    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    return {"Authorization": f"Bearer {jwt}", **agent_headers("sess-box")}


async def _register(
    client: AsyncClient, org_admin: OrgWithAdmin, mt: ComputeMachineType, pod: str
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": mt.provider,
            "provider_pod_id": pod,
            "name": "databench-box",
            "machine_type_code": mt.provider_type_id,
        },
        headers=await _headers(org_admin),
    )
    assert resp.status_code in (200, 201), resp.text
    body: dict[str, object] = resp.json()
    return body


async def _org_machines(session: AsyncSession, org_id: UUID) -> list[OrgMachine]:
    rows = await session.execute(select(OrgMachine).where(OrgMachine.org_team_id == org_id))
    return list(rows.scalars().all())


async def test_a_self_hosted_box_becomes_the_orgs_added_machine(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session, provider=SELF_HOSTED, available_for_new=False)
    await make_grant(
        real_session,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        rate_per_minute_nanos=500,
    )

    body = await _register(client, org_admin, mt, "local-databench-box")

    (machine,) = await _org_machines(real_session, org_admin.org_id)
    assert (machine.name, machine.acquisition, machine.use_mode, machine.free_until) == (
        "databench-box",
        "added",
        "pool",
        None,
    )
    assert machine.owner_team_id == org_admin.org_id
    alloc = await real_session.get(ComputeAllocation, UUID(str(body["id"])))
    assert alloc is not None
    assert (alloc.org_machine_id, alloc.tenant_org_id) == (machine.id, org_admin.org_id)
    assert machine.current_allocation_id == alloc.id
    # Nothing is charged for a machine the org runs itself, whatever the grant says.
    assert alloc.price_per_minute_nanos == 0
    offering = await real_session.get(ComputeOffering, machine.offering_id)
    assert offering is not None
    assert (
        offering.machine_type_id,
        offering.purchasable,
        offering.fixed_rate_per_minute_nanos,
    ) == (
        mt.id,
        False,
        0,
    )


async def test_registering_again_keeps_the_one_org_machine(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session, provider=SELF_HOSTED, available_for_new=False)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)

    first = await _register(client, org_admin, mt, "local-box")
    second = await _register(client, org_admin, mt, "local-box")

    assert first["id"] == second["id"]
    (machine,) = await _org_machines(real_session, org_admin.org_id)
    assert str(machine.current_allocation_id) == first["id"]


async def test_a_name_another_org_machine_holds_steps_to_the_next_free_one(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session, provider=SELF_HOSTED, available_for_new=False)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=2)

    await _register(client, org_admin, mt, "pod-aaaaaaaa")
    await _register(client, org_admin, mt, "pod-bbbbbbbb")

    names = sorted(m.name for m in await _org_machines(real_session, org_admin.org_id))
    assert names == ["databench-box", "databench-box 2"]


async def test_a_provider_box_registers_without_an_org_machine(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: only a box that registers itself is adopted."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)

    body = await _register(client, org_admin, mt, "pod-runpod")

    assert await _org_machines(real_session, org_admin.org_id) == []
    alloc = await real_session.get(ComputeAllocation, UUID(str(body["id"])))
    assert alloc is not None
    assert (alloc.org_machine_id, alloc.tenant_org_id) == (None, None)


async def test_the_org_machine_reconcile_never_launches_a_self_hosted_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """A box that registers itself and was reaped comes back by registering
    again: the reconcile must not replace it with a pending allocation that
    no provider can start."""
    from alkera_core.compute.org_reconcile import reconcile_org_machines

    mt = await make_machine_type(real_session, provider=SELF_HOSTED, available_for_new=False)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    body = await _register(client, org_admin, mt, "local-reaped")
    alloc = await real_session.get(ComputeAllocation, UUID(str(body["id"])))
    assert alloc is not None
    alloc.state = "released"
    await real_session.commit()

    from alkera_core.compute.provider import provider_for_kind
    from alkera_core.config import settings

    async def _no_sleep(_seconds: float) -> None:
        return None

    async with AsyncSessionLocal() as db:
        await reconcile_org_machines(
            db,
            providers=lambda kind: provider_for_kind(kind, settings),
            config=settings,
            sleep=_no_sleep,
            only=[UUID(str(alloc.org_machine_id))],
        )

    real_session.expire_all()
    rows = await real_session.execute(
        select(ComputeAllocation).where(ComputeAllocation.org_team_id == org_admin.org_id)
    )
    assert [a.id for a in rows.scalars().all()] == [alloc.id]
