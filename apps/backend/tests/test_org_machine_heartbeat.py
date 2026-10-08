"""A box backing an org machine says when it last worked and what GPUs it has,
and is told what machine it is.

Through the real heartbeat and claim routes, on the box's own machine
credential. The card is answered only to the box of an org machine in the
credential's own org; every other box gets the bodiless answer it always had.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import ComputeAllocation
from backend.services.credentials import machine_credentials as machine_credential_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests._compute_helpers import make_machine_type
from tests._org_machine_rows import make_org_machine
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

GPUS = [
    {
        "index": 0,
        "name": "NVIDIA A40",
        "memory_used_bytes": 1048576,
        "memory_total_bytes": 48304947200,
        "utilization_percent": 97.0,
    }
]


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


async def _credential_for(alloc: ComputeAllocation, *, org_id: UUID, admin_id: UUID) -> str:
    async with AsyncSessionLocal() as session:
        from alkera_core.models.compute import ComputeMachineType

        machine_type = await session.get(ComputeMachineType, alloc.machine_type_id)
        assert machine_type is not None
        credential, raw = await machine_credential_service.mint(
            session,
            org_id=org_id,
            created_by=admin_id,
            machine_type=machine_type,
            tenancy="dedicated",
            label="box",
        )
        credential.machine_id = alloc.id
        await session.commit()
        return raw


async def _gpu_org_machine(
    org_admin: OrgWithAdmin, *, price: int = 7, idle: int | None = 30
) -> tuple[ComputeAllocation, str]:
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session, compute_class="gpu")
        machine_type.gpu_name = "A40"
        machine_type.gpu_memory_gb = 48
        await session.commit()
        _om, alloc = await make_org_machine(
            session,
            org_id=org_admin.org_id,
            user_id=org_admin.admin_id,
            machine_type=machine_type,
            name="Trainer",
            price_per_minute_nanos=price,
            idle_stop_minutes=idle,
            storage_gb=120,
        )
    assert alloc is not None
    raw = await _credential_for(alloc, org_id=org_admin.org_id, admin_id=org_admin.admin_id)
    return alloc, raw


async def _row(machine_id: UUID) -> ComputeAllocation:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(ComputeAllocation).where(ComputeAllocation.id == machine_id)
            )
        ).scalar_one()


async def test_a_beat_stores_the_activity_and_the_gpus_and_answers_the_card(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    alloc, raw = await _gpu_org_machine(org_admin)
    worked = datetime.now(UTC) - timedelta(minutes=3)
    resp = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={
            "chats_served": 1,
            "last_activity_at": worked.isoformat(),
            "resources": {"cpu_percent": 3.0, "gpus": GPUS},
        },
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "card": {
            "name": "Trainer",
            "gpu": {"name": "A40", "count": 1, "memory_gb": 48},
            "vcpu": 8,
            "memory_gb": 32,
            "disk_gb": 120,
            "billed_per_minute": True,
            "idle_stop_minutes": 30,
        }
    }
    row = await _row(alloc.id)
    assert row.last_activity_at is not None
    assert abs((row.last_activity_at - worked).total_seconds()) < 1
    assert row.resources_json is not None
    assert row.resources_json["gpus"] == GPUS


async def test_a_comped_machine_is_not_billed_per_minute(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    alloc, raw = await _gpu_org_machine(org_admin, price=0, idle=None)
    resp = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()["card"]
    assert card["billed_per_minute"] is False
    assert card["idle_stop_minutes"] is None
    # Never a price, whatever the machine costs.
    assert set(card) == {
        "name",
        "gpu",
        "vcpu",
        "memory_gb",
        "disk_gb",
        "billed_per_minute",
        "idle_stop_minutes",
    }


async def test_a_stamp_from_a_box_clock_ahead_of_ours_lands_at_now(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    alloc, raw = await _gpu_org_machine(org_admin)
    before = datetime.now(UTC)
    ahead = before + timedelta(hours=6)
    await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={"last_activity_at": ahead.isoformat()},
        headers={"Authorization": f"Bearer {raw}"},
    )
    row = await _row(alloc.id)
    assert row.last_activity_at is not None
    assert row.last_activity_at <= datetime.now(UTC)


async def test_an_older_stamp_or_none_never_moves_the_activity_back(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    alloc, raw = await _gpu_org_machine(org_admin)
    recent = datetime.now(UTC) - timedelta(minutes=1)
    headers = {"Authorization": f"Bearer {raw}"}
    url = f"/api/v1/machines/{alloc.id}/heartbeat"
    await client.post(url, json={"last_activity_at": recent.isoformat()}, headers=headers)
    await client.post(
        url,
        json={"last_activity_at": (recent - timedelta(hours=1)).isoformat()},
        headers=headers,
    )
    await client.post(url, json={}, headers=headers)
    row = await _row(alloc.id)
    assert row.last_activity_at is not None
    assert abs((row.last_activity_at - recent).total_seconds()) < 1


async def test_a_box_backing_no_org_machine_gets_the_bodiless_answer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        alloc = ComputeAllocation(
            user_id=org_admin.admin_id,
            org_team_id=org_admin.org_id,
            machine_type_id=machine_type.id,
            lifecycle="workspace",
            state="ready",
            tenancy="pool",
        )
        session.add(alloc)
        await session.commit()
    async with AsyncSessionLocal() as session:
        credential, raw = await machine_credential_service.mint(
            session,
            org_id=org_admin.org_id,
            created_by=org_admin.admin_id,
            machine_type=machine_type,
            tenancy="pool",
            label="pool box",
        )
        credential.machine_id = alloc.id
        await session.commit()
    resp = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={"last_activity_at": datetime.now(UTC).isoformat()},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 204
    assert resp.content == b""


async def test_the_card_is_never_answered_for_another_orgs_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """An allocation filed in the operator's org but backing a machine of a
    second org: its box, speaking with an operator-org credential, is not told
    the second org's machine."""
    async with AsyncSessionLocal() as session:
        other, _ = await team_service.create_org_with_admin(
            session,
            org_name="Other Org",
            admin_email=f"other-{org_admin.org_id.hex[:8]}@alkera.dev",
            admin_first_name="O",
            admin_last_name="A",
            admin_password="pw-1234567890",
        )
        await session.commit()
        machine_type = await make_machine_type(session, compute_class="gpu")
        _om, alloc = await make_org_machine(
            session,
            org_id=other.id,
            user_id=org_admin.admin_id,
            machine_type=machine_type,
            name="Theirs",
        )
        assert alloc is not None
        alloc.org_team_id = org_admin.org_id
        await session.commit()
    raw = await _credential_for(alloc, org_id=org_admin.org_id, admin_id=org_admin.admin_id)
    resp = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 204, resp.text
    assert b"Theirs" not in resp.content


async def test_the_claim_answer_carries_the_card(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box of a machine the reconcile started claims it as it comes up,
    and learns what it is in the same answer."""
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session, compute_class="gpu")
        _om, alloc = await make_org_machine(
            session,
            org_id=org_admin.org_id,
            user_id=org_admin.admin_id,
            machine_type=machine_type,
            name="Trainer",
            state="bootstrapping",
            # The provision recorded the pod's id before the box came up.
            provider_machine_id="pod-trainer",
        )
    assert alloc is not None
    raw = await _credential_for(alloc, org_id=org_admin.org_id, admin_id=org_admin.admin_id)
    resp = await client.post(
        "/api/v1/machines/claim",
        json={
            "provider_pod_id": "pod-trainer",
            "name": "Trainer",
            "capacity": 2,
            "daemon_version": "9",
        },
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(alloc.id)
    assert resp.json()["card"]["name"] == "Trainer"


async def test_a_beat_that_says_nothing_of_gpus_keeps_the_ones_reported(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A daemon too old to sample GPUs (or one restarted before its first
    sample) must not erase what an earlier beat reported; a beat that does
    report GPUs replaces them."""
    alloc, raw = await _gpu_org_machine(org_admin)
    headers = {"Authorization": f"Bearer {raw}"}
    url = f"/api/v1/machines/{alloc.id}/heartbeat"
    await client.post(url, json={"resources": {"cpu_percent": 1.0, "gpus": GPUS}}, headers=headers)

    older = {"cpu_percent": 9.0, "memory_used_bytes": 7}
    resp = await client.post(url, json={"resources": older}, headers=headers)
    assert resp.status_code == 200, resp.text
    row = await _row(alloc.id)
    assert row.resources_json is not None
    assert row.resources_json["gpus"] == GPUS
    assert row.resources_json["cpu_percent"] == 9.0

    busier = [{**GPUS[0], "utilization_percent": 12.0}]
    await client.post(
        url, json={"resources": {"cpu_percent": 2.0, "gpus": busier}}, headers=headers
    )
    row = await _row(alloc.id)
    assert row.resources_json is not None
    assert row.resources_json["gpus"] == busier
