"""An admin's drain, undrain or terminate acts on the row as it is now.

The reconcile moves a platform box on its own schedule. A route that read the
row, then wrote an edge checked against what it read, would overwrite a move
the reconcile committed in between — an undrain turning ``releasing`` back into
``ready`` for a machine that is shutting down. Here the reconcile's move is
held open in a second session while the admin's request runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
from alkera_core.compute.provider import EC2
from alkera_core.compute.transitions import transition
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, OrgComputeAssignment
from alkera_core.models.compute import ComputeAllocation, ComputeAllocationEvent
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.compute import provisioning
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

MACHINES = "/admin/v1/machines"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNodeProvider]:
    provider = FakeNodeProvider(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: provider)
    yield provider


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()


async def _box_in(
    client: AsyncClient, admin: OrgWithAdmin, session: AsyncSession, *path: str
) -> UUID:
    mt = await make_machine_type(session, provider=EC2)
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(
        f"{MACHINES}/provision",
        json={
            "provider": "ec2",
            "machine_type_code": mt.provider_type_id,
            "storage_gb": 100,
            "tenancy": "pool",
            "name": "pool-a",
        },
    )
    assert resp.status_code == 202, resp.text
    alloc_id = UUID(resp.json()["id"])
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, alloc_id)
        assert alloc is not None
        for state in path:
            transition(db, alloc, state)
        await db.commit()
    return alloc_id


async def _race(client: AsyncClient, alloc_id: UUID, to: str, route: str) -> tuple[int, str]:
    """The reconcile holds the row and moves it to ``to``; the admin's
    ``route`` runs meanwhile; then the reconcile commits."""
    async with AsyncSessionLocal() as reconcile:
        alloc = (
            await reconcile.execute(
                select(ComputeAllocation).where(ComputeAllocation.id == alloc_id).with_for_update()
            )
        ).scalar_one()
        transition(reconcile, alloc, to, reason="drained empty")
        await reconcile.flush()
        request = asyncio.create_task(client.post(f"{MACHINES}/{alloc_id}/{route}", json={}))
        await asyncio.sleep(0.5)
        await reconcile.commit()
    resp = await asyncio.wait_for(request, timeout=10)
    return resp.status_code, resp.text


async def _state_and_edges(alloc_id: UUID) -> tuple[str, list[tuple[str, str]]]:
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, alloc_id)
        assert alloc is not None
        edges = (
            await db.execute(
                select(ComputeAllocationEvent.from_state, ComputeAllocationEvent.to_state)
                .where(ComputeAllocationEvent.allocation_id == alloc_id)
                .order_by(ComputeAllocationEvent.at)
            )
        ).all()
        return alloc.state, [(a, b) for a, b in edges]


async def test_an_undrain_does_not_undo_a_release_the_reconcile_committed_meanwhile(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    alloc_id = await _box_in(
        client, platform_admin, real_session, "bootstrapping", "ready", "draining"
    )
    status, body = await _race(client, alloc_id, "releasing", "undrain")
    assert status == 409, body
    assert "not_draining" in body
    state, edges = await _state_and_edges(alloc_id)
    assert state == "releasing"
    assert edges[-1] == ("draining", "releasing")


async def test_a_drain_does_not_land_on_a_box_the_reconcile_released_meanwhile(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    alloc_id = await _box_in(client, platform_admin, real_session, "bootstrapping", "ready")
    status, body = await _race(client, alloc_id, "releasing", "drain")
    assert status == 409, body
    assert (await _state_and_edges(alloc_id))[0] == "releasing"


async def test_a_terminate_racing_a_release_records_one_release(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
) -> None:
    alloc_id = await _box_in(client, platform_admin, real_session, "bootstrapping", "ready")
    status, body = await _race(client, alloc_id, "releasing", "terminate")
    assert status == 409, body
    state, edges = await _state_and_edges(alloc_id)
    assert state == "releasing"
    assert [e for e in edges if e[1] == "releasing"] == [("ready", "releasing")]
