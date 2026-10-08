"""API email-verification gate (`require_verified_or_grace`).

A blocked account (unverified past its grace window) is refused on the product
routers but can still reach the open surfaces the SPA's full-screen gate needs:
`/auth/me`, resend, logout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.config import settings
from alkera_core.models import PlatformRole, User
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

# A representative gated product route (requires auth + the verification gate).
GATED_ROUTE = "/api/v1/dashboard"


async def _age_account(session: AsyncSession, user: User, *, days_ago: int) -> None:
    """Backdate `created_at` so the grace window is in the past (or far future)."""
    user.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    await session.commit()


async def test_verified_user_passes_gate(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _age_account(real_session, member, days_ago=999)  # verified → never gated
    await login(client, member.email, pw)
    resp = await client.get(GATED_ROUTE)
    assert resp.status_code == 200, resp.text


async def test_grace_user_passes_gate(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # Unverified but freshly created → inside the grace window → allowed.
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, pw)
    resp = await client.get(GATED_ROUTE)
    assert resp.status_code == 200, resp.text


async def test_unverified_past_grace_is_blocked(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, pw)  # login (auth router) is never gated
    await _age_account(
        real_session, member, days_ago=settings.email_verification_grace_period_days + 1
    )
    resp = await client.get(GATED_ROUTE)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_verification_required"


async def test_blocked_user_can_still_reach_open_surfaces(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch_verification_send: list[dict],
) -> None:
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, pw)
    await _age_account(
        real_session, member, days_ago=settings.email_verification_grace_period_days + 5
    )

    # /auth/me stays open so the SPA can render the gate (and surfaces the state).
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email_verification_required"] is True
    assert body["email_verification_deadline"] is not None

    # Resend stays open so a blocked user can request a fresh link.
    resend = await client.post("/api/v1/auth/verify-email/resend")
    assert resend.status_code == 200
    assert any(s["email"] == member.email for s in monkeypatch_verification_send)

    # Logout stays open.
    out = await client.post("/api/v1/auth/logout")
    assert out.status_code == 200


async def test_blocked_user_gated_on_individually_gated_invite_route(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # The recipient-scoped `/invitations/me` opts into the gate via `VerifiedUser`
    # (its router can't be blanket-gated — it also serves the PUBLIC invite
    # preview). A blocked user must still be refused here.
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, pw)
    await _age_account(
        real_session, member, days_ago=settings.email_verification_grace_period_days + 1
    )
    resp = await client.get("/api/v1/invitations/me")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_verification_required"


async def test_platform_staff_exempt_from_gate(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # Old + unverified, but platform staff are provisioned without a real inbox.
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    member.platform_role = PlatformRole.ALKERA_SUPPORT
    await _age_account(real_session, member, days_ago=999)
    await login(client, member.email, pw)
    resp = await client.get(GATED_ROUTE)
    assert resp.status_code == 200, resp.text
