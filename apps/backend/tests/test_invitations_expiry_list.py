"""An invitation past its ``expires_at`` is never listed as pending.

The team's pending list read every row still marked pending, so an
invitation that could no longer be accepted stayed on it — late on its
expiry day a reader saw it offered as pending. The list is driven across the
boundary with a frozen clock: a second before, it is pending; a second after,
it is gone from the list and filed as expired.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Invitation
from freezegun import freeze_time
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login


async def _pending(client: AsyncClient, org_admin: OrgWithAdmin, team_id: str) -> list[str]:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    listed = await client.get(f"/api/v1/teams/{team_id}/invitations")
    assert listed.status_code == 200, listed.text
    return [row["id"] for row in listed.json()]


@pytest.mark.asyncio
async def test_an_invitation_leaves_the_pending_list_the_moment_it_expires(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send: Any
) -> None:
    with freeze_time("2026-09-24 20:00:00", real_asyncio=True) as frozen:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        team = await client.post("/api/v1/teams", json={"name": "Expiring"})
        assert team.status_code == 201, team.text
        team_id = team.json()["id"]
        created = await client.post(
            f"/api/v1/teams/{team_id}/invitations",
            json={"email": f"late-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
        )
        assert created.status_code == 201, created.text
        invitation_id = created.json()["id"]
        expires_at = datetime.fromisoformat(created.json()["expires_at"])

        frozen.move_to(expires_at - timedelta(seconds=1))
        assert invitation_id in await _pending(client, org_admin, team_id)

        frozen.move_to(expires_at + timedelta(seconds=1))
        assert invitation_id not in await _pending(client, org_admin, team_id)

    async with AsyncSessionLocal() as session:
        row = await session.get(Invitation, UUID(invitation_id))
        assert row is not None
        assert row.status.value == "expired"
