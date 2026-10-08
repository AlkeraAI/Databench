"""Org settings routes: member read, org-admin write, admin override."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


async def test_member_can_read_defaults(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["allow_login_google"] is True
    assert body["allow_login_github"] is True


async def test_org_admin_can_update(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put("/api/v1/org/settings", json={"allow_login_github": False})
    assert resp.status_code == 200
    assert resp.json()["allow_login_github"] is False
    # Google untouched (partial update).
    assert resp.json()["allow_login_google"] is True

    # Persisted.
    again = await client.get("/api/v1/org/settings")
    assert again.json()["allow_login_github"] is False


async def test_non_admin_member_cannot_update(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, password or "")
    resp = await client.put("/api/v1/org/settings", json={"allow_login_google": False})
    assert resp.status_code == 403


async def test_support_can_read_but_not_write_org_settings(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    # SUPPORT-tier staff can VIEW any org's settings...
    await login(client, platform_support.admin_email, platform_support.admin_password)
    get_resp = await client.get(f"/admin/v1/orgs/{org_admin.org_id}/settings")
    assert get_resp.status_code == 200
    # ...but NOT change login policy (locking a tenant out is ADMIN-only).
    put_resp = await client.put(
        f"/admin/v1/orgs/{org_admin.org_id}/settings",
        json={"allow_login_google": False},
    )
    assert put_resp.status_code == 403


async def test_platform_admin_can_write_org_settings(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    put_resp = await client.put(
        f"/admin/v1/orgs/{org_admin.org_id}/settings",
        json={"allow_login_google": False},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["allow_login_google"] is False


async def test_get_settings_does_not_persist_a_row(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # A plain read must NOT write an OrgSettings row (no surprise INSERT / PK race).
    from alkera_core.models import OrgSettings
    from sqlalchemy import select

    # Remove the row auto-created at org bootstrap so we can observe read behavior.
    query = select(OrgSettings).where(OrgSettings.org_team_id == org_admin.org_id)
    existing = (await real_session.execute(query)).scalar_one_or_none()
    if existing is not None:
        await real_session.delete(existing)
        await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json()["allow_login_google"] is True

    query = select(OrgSettings).where(OrgSettings.org_team_id == org_admin.org_id)
    row = (await real_session.execute(query)).scalar_one_or_none()
    assert row is None, "GET /org/settings must not materialize a row"


async def test_settings_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/org/settings")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# web_search_enabled — the org web-tools toggle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("self_hosted", "expected_default"),
    [
        pytest.param(False, True, id="saas-defaults-on"),
        pytest.param(True, False, id="self-hosted-defaults-off"),
    ],
)
async def test_web_search_default_follows_deployment(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    self_hosted: bool,
    expected_default: bool,
) -> None:
    """An unset (NULL) toggle resolves to the deployment default — on for the
    Alkera-operated SaaS, off for self-hosted — at READ time, so the same stored
    row reads correctly on either deployment."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "self_hosted", self_hosted)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json()["web_search_enabled"] is expected_default


async def test_web_search_explicit_choice_wins_over_deployment_default(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "self_hosted", True)  # deployment default: OFF
    await login(client, org_admin.admin_email, org_admin.admin_password)

    resp = await client.put("/api/v1/org/settings", json={"web_search_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["web_search_enabled"] is True  # explicit ON wins on self-hosted

    # An update that omits the field leaves the choice untouched.
    resp = await client.put("/api/v1/org/settings", json={"allow_login_github": True})
    assert resp.json()["web_search_enabled"] is True

    # An explicit null RESETS to the deployment default (off here).
    resp = await client.put("/api/v1/org/settings", json={"web_search_enabled": None})
    assert resp.json()["web_search_enabled"] is False


async def test_web_search_explicit_off_wins_on_saas(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "self_hosted", False)  # deployment default: ON
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put("/api/v1/org/settings", json={"web_search_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["web_search_enabled"] is False


# ---------------------------------------------------------------------------
# ownership_escalation_enabled -- the cross-team escalation opt-in
# ---------------------------------------------------------------------------


async def test_ownership_escalation_defaults_off_on_member_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, password or "")
    resp = await client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json()["ownership_escalation_enabled"] is False


async def test_org_admin_enables_ownership_escalation(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put("/api/v1/org/settings", json={"ownership_escalation_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["ownership_escalation_enabled"] is True

    # Persisted.
    again = await client.get("/api/v1/org/settings")
    assert again.json()["ownership_escalation_enabled"] is True


async def test_member_cannot_enable_ownership_escalation(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, password or "")
    resp = await client.put("/api/v1/org/settings", json={"ownership_escalation_enabled": True})
    assert resp.status_code == 403

    # The refused write left the setting standing.
    still = await client.get("/api/v1/org/settings")
    assert still.json()["ownership_escalation_enabled"] is False


@pytest.mark.parametrize("value", [True, False], ids=["on", "off"])
@pytest.mark.parametrize("as_staff", [True, False], ids=["platform-admin", "org-admin"])
async def test_the_retired_live_lane_switch_is_ignored_and_never_read_back(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    as_staff: bool,
    value: bool,
) -> None:
    """Every org is on the live draft lane, so no setting names it. A body
    still carrying the old switch is treated like any unknown field — the rest
    of it applies — and neither the write nor a read answers with it."""
    who = platform_admin if as_staff else org_admin
    await login(client, who.admin_email, who.admin_password)
    url = f"/admin/v1/orgs/{org_admin.org_id}/settings" if as_staff else "/api/v1/org/settings"
    put = await client.put(
        url, json={"chat_workspace_crdt_enabled": value, "allow_login_github": False}
    )
    assert put.status_code == 200, put.text
    assert put.json()["allow_login_github"] is False
    assert "chat_workspace_crdt_enabled" not in put.json()
    read = await client.get(url)
    assert read.status_code == 200
    assert "chat_workspace_crdt_enabled" not in read.json()
