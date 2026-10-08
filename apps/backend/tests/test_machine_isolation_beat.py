"""What a box says on its heartbeat about keeping orgs apart and about a
worker that cannot start, through the real routes.

The RunPod box this came from read ready while every turn stayed pending: its
org worker could not start, the only trace was in the pod's own log, and the
console showed nothing. A beat now carries the box's isolation and, while no
worker of it can serve, its fault: both reach the row, the platform console
reads them, and placement holds the box to the profile its capabilities
state.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.compute.worker_faults import UNHEALTHY_MESSAGE
from backend.services.compute import placement
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login
from tests.test_machine_principal_routes import Box, _box, _row

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

RUNPOD_BEAT: dict[str, Any] = {
    "capabilities": [BoxCapability.ORG_WORKERS],
    "isolation": {"profile": "single_org", "mechanisms": []},
    "fault": {
        "code": "cgroup_refused",
        "summary": "could not make org 0's cgroup: /bin/sh -c (I/O error)",
    },
}


async def _beat(client: AsyncClient, box: Box, **body: Any) -> None:
    resp = await client.post(
        f"/api/v1/machines/{box.machine_id}/heartbeat", json=body, headers=box.headers
    )
    assert resp.status_code in (200, 204), resp.text


async def test_a_runpod_beat_reaches_the_console_as_one_org_and_unhealthy(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(platform_admin, tenancy="pool")
    await _beat(client, box, **RUNPOD_BEAT)

    row = await _row(box.machine_id)
    assert row.fault_code == "cgroup_refused"
    assert row.fault_since is not None and row.fault_until is None
    assert not placement.isolates_orgs(row)

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    detail = await client.get(f"/admin/v1/machines/{box.machine_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["isolation"] == {"profile": "single_org", "mechanisms": []}
    assert body["fault"]["code"] == "cgroup_refused"
    assert body["fault"]["message"] == UNHEALTHY_MESSAGE
    assert body["fault"]["summary"] == "could not make org 0's cgroup: /bin/sh -c (I/O error)"


async def test_a_beat_that_serves_again_ends_the_fault(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(platform_admin, tenancy="pool")
    await _beat(client, box, **RUNPOD_BEAT)
    await _beat(client, box, capabilities=[BoxCapability.ORG_WORKERS])
    row = await _row(box.machine_id)
    assert row.fault_code is None
    assert row.fault_since is not None and row.fault_until is not None
    assert row.fault_until >= row.fault_since
    assert row.isolation_json is None  # a beat that says nothing clears it


@pytest.mark.parametrize(
    ("capabilities", "profile", "isolates"),
    [
        pytest.param(
            [BoxCapability.ORG_WORKERS, BoxCapability.ORG_ISOLATION],
            "org_namespaces",
            True,
            id="an-ec2-box",
        ),
        # The report's own word is not what placement reads: a box that claims
        # namespaces without the capability is one org's.
        pytest.param([BoxCapability.ORG_WORKERS], "single_org", False, id="claims-without-proof"),
    ],
)
async def test_placement_and_the_console_read_the_profile_from_the_capabilities(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    capabilities: list[str],
    profile: str,
    isolates: bool,
) -> None:
    box = await _box(platform_admin, tenancy="pool")
    await _beat(
        client,
        box,
        capabilities=capabilities,
        isolation={"profile": "org_namespaces", "mechanisms": ["cgroup_delegation"]},
    )
    row = await _row(box.machine_id)
    assert placement.isolates_orgs(row) is isolates
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    detail = (await client.get(f"/admin/v1/machines/{box.machine_id}")).json()
    assert detail["isolation"]["profile"] == profile


async def test_a_fault_with_a_code_this_backend_does_not_know_reads_as_other(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    box = await _box(platform_admin, tenancy="pool")
    await _beat(client, box, fault={"code": "from_a_later_build", "summary": "x"})
    assert (await _row(box.machine_id)).fault_code == "other"
