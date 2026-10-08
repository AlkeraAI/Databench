"""Auth route integration tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login


@pytest.mark.asyncio
async def test_login_happy_path_sets_cookie(client: AsyncClient, org_admin: OrgWithAdmin):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": org_admin.admin_password},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == org_admin.admin_email
    assert "expires_at" in body
    # Cookie should be set on the client.
    assert "alkera_session" in client.cookies


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(client: AsyncClient, org_admin: OrgWithAdmin):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": "WRONG"},
    )
    assert resp.status_code == 401
    assert "alkera_session" not in client.cookies


@pytest.mark.asyncio
async def test_login_unknown_email_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@alkera.dev", "password": "anything"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_cookie(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_authenticated_user(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == org_admin.admin_email


@pytest.mark.asyncio
async def test_logout_clears_cookie(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert "alkera_session" in client.cookies
    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 200
    # Cookie cleared in the response — client may or may not strip it.
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401
