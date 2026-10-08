"""Admin org-structure endpoints — team tree, org-wide members, rename, delete."""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


@pytest.mark.asyncio
async def test_admin_list_org_teams(
    client: AsyncClient, platform_support: OrgWithAdmin, real_session
):
    from backend.services.org import teams as team_service

    await team_service.create_subteam(
        real_session,
        org_team_id=platform_support.org_id,
        name="Eng",
        parent_team_id=platform_support.org_id,
    )
    await real_session.commit()
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(f"/admin/v1/orgs/{platform_support.org_id}/teams")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert "Eng" in names
    root = next(t for t in resp.json() if t["is_root"])
    assert root["member_count"] >= 1  # the org admin is materialized into the root


@pytest.mark.asyncio
async def test_admin_list_org_members(
    client: AsyncClient, platform_support: OrgWithAdmin, real_session
):
    member, _ = await make_member(real_session, org_id=platform_support.org_id)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(f"/admin/v1/orgs/{platform_support.org_id}/members")
    assert resp.status_code == 200
    emails = {r["email"] for r in resp.json()}
    assert member.email in emails
    assert platform_support.admin_email in emails


@pytest.mark.asyncio
async def test_admin_org_structure_forbidden_for_non_staff(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/teams")).status_code == 403
    assert (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/members")).status_code == 403


@pytest.mark.asyncio
async def test_admin_rename_org(client: AsyncClient, platform_support: OrgWithAdmin):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.patch(
        f"/admin/v1/orgs/{platform_support.org_id}", json={"name": "Renamed Org"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Org"


@pytest.mark.asyncio
async def test_admin_delete_org_cascades(
    client: AsyncClient, platform_admin: OrgWithAdmin, real_session
):
    from backend.services.org import teams as team_service

    suffix = secrets.token_hex(4)
    org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Doomed {suffix}",
        admin_email=f"doomed-{suffix}@alkera.dev",
        admin_first_name="Doomed",
        admin_last_name="Admin",
        admin_password="x" * 16,
    )
    await real_session.commit()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.delete(f"/admin/v1/orgs/{org.id}")
    assert resp.status_code == 204
    assert (await client.get(f"/admin/v1/orgs/{org.id}")).status_code == 404
