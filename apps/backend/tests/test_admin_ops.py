"""The admin ops summary: every live machine with its provider, state and
chats, spend against the platform cap, crash reports, deployment health.

Staff-only, and the health run is bounded: a hung check turns into one failed
row instead of a hung page.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.compute.provider import EC2, RUNPOD
from alkera_core.deployment_health import CheckContext, CheckResult
from alkera_core.models import WorkspaceObject
from alkera_core.models.compute import ORG_TENANCY, ComputeAllocation
from backend.services.ops import ops_summary as ops_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

URL = "/admin/v1/ops/summary"


async def _ok(_ctx: CheckContext) -> list[CheckResult]:
    return [CheckResult("postgres", "Postgres", "ok", "reachable")]


async def _warn(_ctx: CheckContext) -> list[CheckResult]:
    return [CheckResult("smtp", "SMTP", "warn", "slow"), CheckResult("x", "X", "skipped", "")]


async def _hangs(_ctx: CheckContext) -> list[CheckResult]:
    await asyncio.sleep(5)
    return []


@pytest.fixture(autouse=True)
def _fake_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real registry probes providers, SMTP and Temporal; the summary's
    wiring is what is under test, so it runs two local checks."""
    monkeypatch.setattr(ops_service, "HEALTH_CHECKS", (_ok, _warn))


async def _box(
    session: AsyncSession, org: OrgWithAdmin, *, name: str, provider: str, beat: datetime | None
) -> ComputeAllocation:
    machine_type = await make_machine_type(session, provider=provider)
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        tenancy=ORG_TENANCY,
        state="ready",
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=beat,
    )
    session.add(alloc)
    await session.commit()
    return alloc


async def _bind_chat(session: AsyncSession, org: OrgWithAdmin, machine: ComputeAllocation) -> None:
    async with app_client() as tenant:
        await login(tenant, org.admin_email, org.admin_password)
        created = await tenant.post("/api/v1/chats", json={"title": "Bound"})
        assert created.status_code == 201, created.text
    chat = await session.get(WorkspaceObject, UUID(created.json()["id"]))
    assert chat is not None
    chat.spec = {**chat.spec, "machine_id": str(machine.id)}
    flag_modified(chat, "spec")
    await session.commit()


async def _summary(client: AsyncClient) -> dict[str, Any]:
    resp = await client.get(URL)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_machines_carry_provider_state_and_chats_served(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    ready = await _box(
        real_session, org_admin, name="ops-ready", provider=EC2, beat=datetime.now(UTC)
    )
    stale = await _box(
        real_session,
        org_admin,
        name="ops-stale",
        provider=RUNPOD,
        beat=datetime.now(UTC) - timedelta(hours=1),
    )
    await _bind_chat(real_session, org_admin, ready)
    await _bind_chat(real_session, org_admin, ready)

    await login(client, platform_support.admin_email, platform_support.admin_password)
    body = await _summary(client)
    by_id = {m["id"]: m for m in body["machines"]}
    assert by_id[str(ready.id)]["provider"] == EC2
    assert by_id[str(ready.id)]["state"] == "ready"
    assert by_id[str(ready.id)]["chats_served"] == 2
    assert by_id[str(stale.id)]["provider"] == RUNPOD
    assert by_id[str(stale.id)]["state"] == "unreachable"
    assert by_id[str(stale.id)]["chats_served"] == 0

    # The strips and the total are the same machines, counted.
    states = {c["key"]: c["count"] for c in body["machines_by_state"]}
    providers = {c["key"]: c["count"] for c in body["machines_by_provider"]}
    assert sum(states.values()) == sum(providers.values()) == len(body["machines"])
    assert states["ready"] >= 1 and states["unreachable"] >= 1
    assert body["chats_served_total"] == sum(m["chats_served"] for m in body["machines"])


async def test_a_released_machine_is_not_listed(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    gone = await _box(real_session, org_admin, name="ops-gone", provider=EC2, beat=None)
    gone.state = "released"
    await real_session.commit()
    await login(client, platform_support.admin_email, platform_support.admin_password)
    assert str(gone.id) not in {m["id"] for m in (await _summary(client))["machines"]}


async def test_crash_reports_count_the_last_day_and_the_unread(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    before = await _summary(client)
    async with app_client() as tenant:
        await login(tenant, org_admin.admin_email, org_admin.admin_password)
        sent = await tenant.post(
            "/api/v1/errors/reports", json={"component": "web", "message": "boom"}
        )
        assert sent.status_code == 201, sent.text
    after = await _summary(client)
    assert after["crash_reports_24h"] == before["crash_reports_24h"] + 1
    assert after["crash_reports_unread"] == before["crash_reports_unread"] + 1


async def test_spend_headroom_is_the_cap_less_the_month(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    spend = (await _summary(client))["spend"]
    assert spend["cap_headroom_nanos"] == spend["monthly_cap_nanos"] - spend["month_to_date_nanos"]
    assert spend["today_nanos"] >= 0


async def test_health_reports_the_worst_status_and_the_build(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    body = await _summary(client)
    assert body["health"]["overall"] == "warn"
    assert [c["key"] for c in body["health"]["checks"]] == ["postgres", "smtp", "x"]
    assert "app_version" in body and "build_id" in body


async def test_a_hung_check_is_one_failed_row_not_a_hung_page(
    client: AsyncClient, platform_support: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops_service, "HEALTH_CHECKS", (_ok, _hangs))
    monkeypatch.setattr(ops_service, "HEALTH_BUDGET_S", 0.2)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    health = (await _summary(client))["health"]
    assert health["overall"] == "fail"
    assert [(c["key"], c["status"]) for c in health["checks"]] == [("run", "fail")]


async def test_an_org_admin_who_is_not_staff_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(URL)).status_code == 403
