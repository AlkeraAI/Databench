"""The name an admin gives a machine is held to a plain charset at the route.

It reaches the node's boot script and its environment file; the script quotes
it, and the route refuses anything outside letters, digits, spaces, dots,
dashes and underscores before a row, a credential or a machine exists.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator

import pytest
from alkera_core.compute.provider import EC2
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, OrgComputeAssignment
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.compute import provisioning
from httpx import AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


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


async def _rows_for(mt: ComputeMachineType) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ComputeAllocation)
                    .where(ComputeAllocation.machine_type_id == mt.id)
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("name", "status", "stored"),
    [
        pytest.param("pool-a", 202, "pool-a", id="plain"),
        pytest.param("Pool box 1", 202, "Pool box 1", id="spaces"),
        pytest.param("  gpu_2.large  ", 202, "gpu_2.large", id="trimmed"),
        pytest.param("a" * 64, 202, "a" * 64, id="64-chars"),
        pytest.param("a" * 65, 400, None, id="65-chars"),
        pytest.param("box $(id)", 400, None, id="command-substitution"),
        pytest.param("box `id`", 400, None, id="backticks"),
        pytest.param("it's", 400, None, id="quote"),
        pytest.param("box;reboot", 400, None, id="semicolon"),
        pytest.param("box\nENV", 400, None, id="newline"),
        pytest.param("-leading-dash", 400, None, id="leading-dash"),
        pytest.param("café", 400, None, id="non-ascii"),
    ],
)
async def test_the_machine_name_is_held_to_a_plain_charset(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    fake: FakeNodeProvider,
    name: str,
    status: int,
    stored: str | None,
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(
        "/admin/v1/machines/provision",
        json={
            "provider": "ec2",
            "machine_type_code": mt.provider_type_id,
            "storage_gb": 100,
            "tenancy": "pool",
            "name": name,
        },
    )
    assert resp.status_code == status, resp.text
    if stored is None:
        assert resp.json()["error"]["code"] == "bad_machine_name"
        assert await _rows_for(mt) == 0
        assert fake.nodes == {}
        return
    assert resp.json()["name"] == stored
    (node,) = fake.nodes.values()
    assert f"ALKERA_MACHINE_NAME={shlex.quote(stored)}" in node.launch.script
