"""Org-admin member-plan view — GET /teams/{team_id}/memberships/{user_id}/plan.

A team admin can see how close a member is to their cycle limit (tier + % used +
reset) — but NEVER the member's exact credit balances. Authorized by team-admin
descent; a non-admin member is refused.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


@pytest.mark.asyncio
async def test_org_admin_views_member_plan_obfuscated(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: object
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)  # type: ignore[arg-type]
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/memberships/{member.id}/plan")
    assert resp.status_code == 200
    body = resp.json()
    # ONLY the obfuscated fields, plus the recurring cap this admin wrote — no
    # exact balances of another user.
    assert set(body) == {
        "tier_key",
        "tier_name",
        "pct_used",
        "reset_at",
        "pool_limit_nanos",
        "pool_limit_consumed_nanos",
    }
    assert body["tier_key"] == "free"  # provisioned at signup
    assert body["pct_used"] == 0.0
    # Nobody has capped this member on a team pool, so the cap reads as absent
    # rather than as a zero a client would render as "no allowance left".
    assert body["pool_limit_nanos"] is None
    assert body["pool_limit_consumed_nanos"] is None
    assert "prepaid" not in str(body) and "granted" not in str(body)


@pytest.mark.asyncio
async def test_non_admin_member_cannot_view_a_plan(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: object
) -> None:
    viewer, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)  # type: ignore[arg-type]
    target, _ = await make_member(real_session, org_id=org_admin.org_id)  # type: ignore[arg-type]
    await login(client, viewer.email, pw or "")
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/memberships/{target.id}/plan")
    assert resp.status_code == 403  # require_team_admin denies a plain member


@pytest.mark.asyncio
async def test_member_plan_unknown_user_404(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/memberships/{uuid4()}/plan")
    assert resp.status_code == 404
