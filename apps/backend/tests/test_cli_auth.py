"""CLI auth: Bearer-token authentication.

CLI tokens are now minted by the device-authorization token endpoint (see
`test_device_auth.py`); these tests cover that a minted CLI Bearer token
authenticates the same JWT surface as the cookie session, and that bad/missing
Authorization headers fall through to 401.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, mint_cli_token


@pytest.mark.asyncio
async def test_bearer_authenticates_subsequent_requests(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    token = await mint_cli_token(
        user_id=org_admin.admin_id,
        email=org_admin.admin_email,
        org_team_id=org_admin.org_id,
    )

    # Cookie-less client — only the Bearer header should authenticate.
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == org_admin.admin_email


@pytest.mark.asyncio
async def test_invalid_bearer_returns_401(client: AsyncClient):
    me = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_cookie_session_still_works(client: AsyncClient, org_admin: OrgWithAdmin):
    """Regression: Bearer support must not break the cookie path."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == org_admin.admin_email


@pytest.mark.asyncio
async def test_malformed_authorization_header_falls_through(client: AsyncClient):
    # Wrong scheme → treated as no token → 401 from the missing-creds branch.
    me = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert me.status_code == 401
