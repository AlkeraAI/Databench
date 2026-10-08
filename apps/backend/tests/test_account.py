"""Account-management routes: password parity, set/change password, complete-profile."""

from __future__ import annotations

import pytest
from alkera_core.db.session import AsyncSessionLocal
from backend.services.identity import users as user_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, login

pytestmark = pytest.mark.asyncio


async def test_no_password_and_wrong_password_look_identical(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """An OAuth-only account (no password) must fail login with the EXACT same
    error as a wrong password — never leak that the account has no password."""
    oauth_only_email = _unique_email("oauth-only")
    await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=oauth_only_email,
        first_name="No",
        last_name="Pass",
        password=None,
    )
    await real_session.commit()

    no_password = await client.post(
        "/api/v1/auth/login", json={"email": oauth_only_email, "password": "anything-at-all"}
    )
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": "definitely-not-correct"},
    )
    assert no_password.status_code == 401
    assert wrong_password.status_code == 401
    assert no_password.json()["error"]["message"] == wrong_password.json()["error"]["message"]


async def test_send_password_reset_emails_current_user(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_password_reset_send: list[dict],
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/auth/password/send-reset")
    assert resp.status_code == 200
    assert len(monkeypatch_password_reset_send) == 1
    assert monkeypatch_password_reset_send[0]["email"] == org_admin.admin_email


async def test_send_password_reset_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/password/send-reset")
    assert resp.status_code == 401


async def test_complete_profile_fills_empty_names(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # A user provisioned without a name (the JIT/SSO sentinel).
    email = _unique_email("noname")
    password = "noname-pass-123"
    await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=email,
        first_name="",
        last_name="",
        password=password,
    )
    await real_session.commit()
    await login(client, email, password)

    before = await client.get("/api/v1/auth/me")
    assert before.json()["first_name"] == ""

    resp = await client.post(
        "/api/v1/auth/complete-profile", json={"first_name": "Grace", "last_name": "Hopper"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["first_name"] == "Grace"
    assert body["last_name"] == "Hopper"
    assert body["display_name"] == "Grace Hopper"

    async with AsyncSessionLocal() as session:
        refreshed = await user_service.get_by_email(session, email)
        assert refreshed is not None
        assert refreshed.first_name == "Grace"


async def test_identities_empty_for_password_user(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/auth/identities")
    assert resp.status_code == 200
    assert resp.json()["identities"] == []
