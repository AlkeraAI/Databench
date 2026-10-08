"""Chat sandbox limits: Alkera admins set them per org, the box reads them.

The limits size the machine an org is billed for, so only platform admins
write them; an org admin's body naming them changes nothing. The chat listing
is how a box learns them, so every row it lists carries the org's figures.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.sandbox_tiers import TIER_LIMITS
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login

pytestmark = pytest.mark.asyncio


def _settings_url(org_id: object) -> str:
    return f"/admin/v1/orgs/{org_id}/settings"


async def test_defaults_are_null_meaning_the_box_default(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    body = (await client.get(_settings_url(org_admin.org_id))).json()
    assert body["sandbox_vcpu"] is None
    assert body["sandbox_memory_mb"] is None


async def test_platform_admin_sets_then_resets_one_limit_leaving_the_other(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    url = _settings_url(org_admin.org_id)
    put = await client.put(url, json={"sandbox_vcpu": 8, "sandbox_memory_mb": 16384})
    assert put.status_code == 200, put.text
    assert (put.json()["sandbox_vcpu"], put.json()["sandbox_memory_mb"]) == (8, 16384)

    # An unrelated write leaves both standing.
    other = await client.put(url, json={"allow_login_github": False})
    assert (other.json()["sandbox_vcpu"], other.json()["sandbox_memory_mb"]) == (8, 16384)

    # An explicit null resets only the named limit.
    reset = await client.put(url, json={"sandbox_vcpu": None})
    assert (reset.json()["sandbox_vcpu"], reset.json()["sandbox_memory_mb"]) == (None, 16384)
    again = (await client.get(url)).json()
    assert (again["sandbox_vcpu"], again["sandbox_memory_mb"]) == (None, 16384)


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        pytest.param("sandbox_vcpu", 0, False, id="vcpu-below-min"),
        pytest.param("sandbox_vcpu", 1, True, id="vcpu-min"),
        pytest.param("sandbox_vcpu", 64, True, id="vcpu-max"),
        pytest.param("sandbox_vcpu", 65, False, id="vcpu-above-max"),
        pytest.param("sandbox_memory_mb", 511, False, id="memory-below-min"),
        pytest.param("sandbox_memory_mb", 512, True, id="memory-min"),
        pytest.param("sandbox_memory_mb", 262144, True, id="memory-max"),
        pytest.param("sandbox_memory_mb", 262145, False, id="memory-above-max"),
    ],
)
async def test_limits_are_bounded(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    field: str,
    value: int,
    accepted: bool,
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_settings_url(org_admin.org_id), json={field: value})
    if accepted:
        assert resp.status_code == 200, resp.text
        assert resp.json()[field] == value
    else:
        assert resp.status_code == 422
        assert (await client.get(_settings_url(org_admin.org_id))).json()[field] is None


async def test_support_staff_cannot_set_limits(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.put(_settings_url(org_admin.org_id), json={"sandbox_vcpu": 4})
    assert resp.status_code == 403


async def test_an_org_admin_cannot_set_their_own_limits(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(
        "/api/v1/org/settings",
        json={"sandbox_vcpu": 64, "sandbox_memory_mb": 262144, "allow_login_github": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The rest of the body applied; the limits did not.
    assert body["allow_login_github"] is False
    assert (body["sandbox_vcpu"], body["sandbox_memory_mb"]) == (None, None)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("sandbox_vcpu", 65, id="vcpu"),
        pytest.param("sandbox_memory_mb", 100, id="memory"),
    ],
)
async def test_the_database_refuses_an_out_of_range_limit(
    real_session: AsyncSession, org_admin: OrgWithAdmin, column: str, value: int
) -> None:
    with pytest.raises(IntegrityError):
        await real_session.execute(
            text(f"INSERT INTO org_settings (org_team_id, {column}) VALUES (:org, :value)"),
            {"org": org_admin.org_id, "value": value},
        )
    await real_session.rollback()


async def _list_chats(client: AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/chats")
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


async def test_the_chat_listing_carries_the_org_limits(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post("/api/v1/chats", json={"title": "Sized"})
    assert created.status_code == 201, created.text
    # No override: the row carries the org's plan-tier figure (a fresh org is
    # on the free tier), never nothing.
    [row] = await _list_chats(client)
    assert (row["sandbox_vcpu"], row["sandbox_memory_mb"]) == TIER_LIMITS["free"]

    async with app_client() as staff:
        await login(staff, platform_admin.admin_email, platform_admin.admin_password)
        put = await staff.put(
            _settings_url(org_admin.org_id), json={"sandbox_vcpu": 4, "sandbox_memory_mb": 8192}
        )
        assert put.status_code == 200, put.text

    [row] = await _list_chats(client)
    assert (row["sandbox_vcpu"], row["sandbox_memory_mb"]) == (4, 8192)


async def test_the_listing_carries_only_the_callers_org_limits(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
) -> None:
    # The staff member's own org is sized; the other org is not, and its
    # listing must not pick up a figure that belongs to someone else.
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    put = await client.put(_settings_url(platform_admin.org_id), json={"sandbox_vcpu": 16})
    assert put.status_code == 200, put.text

    async with app_client() as tenant:
        await login(tenant, org_admin.admin_email, org_admin.admin_password)
        assert (await tenant.post("/api/v1/chats", json={"title": "Other"})).status_code == 201
        [row] = await _list_chats(tenant)
        assert row["sandbox_vcpu"] == TIER_LIMITS["free"][0]
