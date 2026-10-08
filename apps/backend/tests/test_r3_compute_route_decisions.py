"""Round-3 review lens (temporal_compute): the heartbeat and current routes
decide through ``enforce()`` and leave an ``authz.decision`` row each — the
existing suite pins the register route's row only."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.authz import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token

pytestmark = pytest.mark.compute_rows


async def _decisions(org_id: Any) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "compute_machine",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def test_heartbeat_and_current_each_leave_a_decision_row(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {jwt}", **agent_headers("sess-r3")}
    reg = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": "pod-r3",
            "name": "r3-box",
            "machine_type_code": mt.provider_type_id,
        },
        headers=headers,
    )
    assert reg.status_code == 201, reg.text
    machine_id = reg.json()["id"]
    assert len(await _decisions(org_admin.org_id)) == 1

    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=headers)
    assert beat.status_code == 204, beat.text
    decisions = await _decisions(org_admin.org_id)
    assert len(decisions) == 2
    assert (decisions[-1].payload["effect"], decisions[-1].payload["reason"]) == (
        "allow",
        "member_write",
    )
    assert decisions[-1].payload["attrs"]["is_owner"] is True
    assert decisions[-1].actor["acting"]["kind"] == "agent"

    await login(client, org_admin.admin_email, org_admin.admin_password)
    cur = await client.get("/api/v1/machines/current")
    assert cur.status_code == 200 and cur.json()["status"] == "ready"
    decisions = await _decisions(org_admin.org_id)
    assert len(decisions) == 3
    assert (decisions[-1].payload["effect"], decisions[-1].payload["reason"]) == (
        "allow",
        "member_read",
    )
    assert decisions[-1].actor["acting"]["kind"] == "user"


async def test_a_denied_heartbeat_is_on_record_as_a_not_found(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    jwt = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    reg = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": "pod-r3-b",
            "name": "r3-box",
            "machine_type_code": mt.provider_type_id,
        },
        headers={"Authorization": f"Bearer {jwt}", **agent_headers("sess-r3")},
    )
    assert reg.status_code == 201, reg.text
    machine_id = reg.json()["id"]
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    other_jwt = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    denied = await client.post(
        f"/api/v1/machines/{machine_id}/heartbeat",
        headers={"Authorization": f"Bearer {other_jwt}", **agent_headers("sess-other")},
    )
    assert denied.status_code == 404, denied.text
    decisions = await _decisions(org_admin.org_id)
    assert (decisions[-1].payload["effect"], decisions[-1].payload["reason"]) == (
        "deny",
        "not_owner",
    )
