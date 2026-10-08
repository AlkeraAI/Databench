"""Admin route tests — covers Support floor + ADMIN-only escalations."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login


@pytest.mark.asyncio
async def test_admin_routes_require_authentication(client: AsyncClient):
    resp = await client.get("/admin/v1/orgs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_routes_deny_regular_user(client: AsyncClient, org_admin: OrgWithAdmin):
    """Org Admin without a platform_role gets 403 on any /admin route."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/admin/v1/orgs")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_support_can_list_orgs(client: AsyncClient, platform_support: OrgWithAdmin):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get("/admin/v1/orgs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_support_can_create_orgs(client: AsyncClient, platform_support: OrgWithAdmin):
    import secrets

    suffix = secrets.token_hex(4)
    org_name = f"New Tenant {suffix}"
    admin_email = f"new-tenant-admin-{suffix}@alkera.dev"
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": org_name,
            "admin_email": admin_email,
            "admin_first_name": "New",
            "admin_last_name": "Admin",
            "admin_password": "vaultkey-12345678",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["org"]["name"] == org_name
    assert body["admin"]["email"] == admin_email


@pytest.mark.asyncio
async def test_support_can_list_users_cross_tenant(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get("/admin/v1/users")
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert org_admin.admin_email in emails


@pytest.mark.asyncio
async def test_support_cannot_delete_org(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
):
    """DELETE /admin/v1/orgs/{id} is ADMIN-only — Support gets 403."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.delete(f"/admin/v1/orgs/{org_admin.org_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_support_cannot_grant_platform_role(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
):
    """Granting platform_role is ADMIN-only — Support gets 403."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": "alkera_admin"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_org_create_cannot_crown_platform_admin(
    client: AsyncClient, platform_support: OrgWithAdmin
):
    """Regression: a SUPPORT staffer must not be able to mint a platform
    admin via org bootstrap. The `admin_platform_role` field is gone — even if
    sent, it's ignored and the new admin is a regular Org Admin."""
    import secrets

    suffix = secrets.token_hex(4)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": f"Tenant {suffix}",
            "admin_email": f"t-{suffix}@alkera.dev",
            "admin_first_name": "T",
            "admin_last_name": "Admin",
            "admin_password": "vaultkey-12345678",
            "admin_platform_role": "alkera_admin",  # ignored — not in the schema
        },
    )
    assert resp.status_code == 201
    assert resp.json()["admin"]["platform_role"] is None  # NOT a platform admin


@pytest.mark.asyncio
async def test_admin_cannot_reset_user_password(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
):
    """Regression: admin/staff can never reset another user's password. The
    password field is gone from the admin update; the old password keeps working
    and the attacker-chosen one is rejected."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}",
        json={"first_name": "Renamed", "password": "hijacked-pw-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Renamed"  # only the cosmetic edit lands

    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": "hijacked-pw-123"},
    )
    assert bad.status_code == 401  # the injected password does NOT work
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": org_admin.admin_password},
    )
    assert ok.status_code == 200  # the original password is unchanged


@pytest.mark.asyncio
async def test_admin_can_grant_platform_role(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": "alkera_support"},
    )
    assert resp.status_code == 200
    assert resp.json()["platform_role"] == "alkera_support"


@pytest.mark.asyncio
async def test_admin_can_clear_platform_role(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    # First grant, then revoke.
    await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": "alkera_admin"},
    )
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": None},
    )
    assert resp.status_code == 200
    assert resp.json()["platform_role"] is None


@pytest.mark.asyncio
async def test_an_org_created_by_staff_mails_its_admin_a_working_verification_link(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    monkeypatch_verification_send: list[dict],
    monkeypatch_welcome_send: list[dict],
):
    """The new admin's address was typed by staff, not proven, so the account is
    born unverified and the same verification mail a bare signup gets goes out
    on creation — never neither. The captured token is a real one: it verifies
    the account through the public route."""
    import secrets

    from tests.conftest import app_client

    suffix = secrets.token_hex(4)
    admin_email = f"staff-made-{suffix}@alkera.dev"
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": f"Staff made {suffix}",
            "admin_email": admin_email,
            "admin_first_name": "Staff",
            "admin_last_name": "Made",
            "admin_password": "vaultkey-12345678",
        },
    )
    assert resp.status_code == 201, resp.text
    admin = resp.json()["admin"]
    assert admin["email_verified_at"] is None
    assert [(m["user_id"], m["email"]) for m in monkeypatch_verification_send] == [
        (admin["id"], admin_email)
    ]

    async with app_client() as anonymous:
        verified = await anonymous.post(
            f"/api/v1/auth/verify-email/{monkeypatch_verification_send[0]['token']}"
        )
    assert verified.status_code == 200, verified.text
    assert verified.json()["email_verified_at"] is not None


@pytest.mark.asyncio
async def test_a_refused_org_creation_sends_no_verification_mail(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    monkeypatch_verification_send: list[dict],
):
    """An address that already has an account is a 409, and nobody is mailed."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": "Duplicate",
            "admin_email": org_admin.admin_email,
            "admin_first_name": "Dup",
            "admin_last_name": "Licate",
            "admin_password": "vaultkey-12345678",
        },
    )
    assert resp.status_code == 409
    assert monkeypatch_verification_send == []
