"""Public /api/v1/config endpoint — branding for the pre-auth SPA."""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.brand import product_name, sales_email, support_email
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import SsoConnection
from httpx import AsyncClient
from sqlalchemy import delete
from tests.conftest import (
    OrgWithAdmin,
    hold_sso_domains,
    login,
    make_org_enterprise,
    signed_in_through_sso,
)


async def _clear_sso_connections() -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(delete(SsoConnection))
        await s.commit()


async def _put_sso(
    client: AsyncClient, domain: str, *, enabled: bool, enforced: bool, org_id: UUID | None = None
) -> None:
    """Configure the org's IdP. Enforcing it takes what the route insists on:
    the connection first, then the admin's own sign-in through it."""
    for step in (False, True) if enforced else (False,):
        if step:
            assert org_id is not None
            await signed_in_through_sso(client, org_id)
        resp = await client.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.acme.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "s",
                "enabled": enabled,
                "enforced": step,
            },
        )
        assert resp.status_code == 200, resp.text
        if not step and org_id is not None:
            await hold_sso_domains(org_id, domain)


@pytest.mark.asyncio
async def test_public_config_returns_defaults(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        # Unset in the test env, so the installed brand names the product.
        "product_name": product_name(),
        "support_email": support_email(),
        "sales_email": sales_email(),
        # The test env has Stripe configured (SaaS) → telemetry allowed.
        "telemetry_enabled": True,
        "self_hosted": False,
        # SaaS never auto-redirects the shared login page to one org's IdP.
        "sso_enforced_login_url": None,
        "workspaces_multi_chat": False,
        "multi_org_enabled": False,
    }


@pytest.mark.asyncio
async def test_public_config_reflects_white_label_overrides(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "brand_product_name", "Acme Data")
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    monkeypatch.setattr(settings, "brand_support_email", "help@acme.example")
    monkeypatch.setattr(settings, "brand_sales_email", "deals@acme.example")
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200
    assert resp.json() == {
        "product_name": "Acme Data",
        "support_email": "help@acme.example",
        "sales_email": "deals@acme.example",
        "telemetry_enabled": True,
        "self_hosted": False,
        "sso_enforced_login_url": None,
        "workspaces_multi_chat": False,
        "multi_org_enabled": False,
    }


@pytest.mark.asyncio
async def test_public_config_is_unauthenticated(client: AsyncClient) -> None:
    # No cookie / bearer — a visitor on the login screen must be able to read it.
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_public_config_disables_telemetry_when_self_hosted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted deployment (no Stripe) must report telemetry_enabled=False so the
    SPA never initializes browser Sentry, even with a DSN baked into the image."""
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)
    monkeypatch.setattr(settings, "self_hosted", None)  # force inference
    assert settings.is_self_hosted is True
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200
    assert resp.json()["telemetry_enabled"] is False


@pytest.mark.asyncio
async def test_enforced_sso_redirect_url_when_self_hosted_single_org(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-hosted instance whose single org enforces SSO surfaces the IdP start
    URL so the login screen can redirect straight to it."""
    await _clear_sso_connections()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # Self-hosted before the PUT: SSO config is Enterprise-gated, and self-hosted
    # installs are entitled — so configuring it here is allowed.
    monkeypatch.setattr(settings, "self_hosted", True)
    await _put_sso(client, "acme.example.com", enabled=True, enforced=True, org_id=org_admin.org_id)

    body = (await client.get("/api/v1/config")).json()
    assert body["sso_enforced_login_url"] is not None
    assert str(org_admin.org_id) in body["sso_enforced_login_url"]


@pytest.mark.asyncio
async def test_enforced_sso_redirect_url_none_on_saas(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SaaS (multi-tenant) never redirects the shared login page — even if an org
    enforces SSO, the per-email discover path handles it instead."""
    await _clear_sso_connections()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # SSO config is Enterprise-gated on SaaS; enroll so this org may configure it,
    # then assert the SaaS behavior (no shared-login redirect) with the plan in place.
    await make_org_enterprise(org_admin.org_id)
    await _put_sso(client, "acme.example.com", enabled=True, enforced=True, org_id=org_admin.org_id)

    monkeypatch.setattr(settings, "self_hosted", False)
    body = (await client.get("/api/v1/config")).json()
    assert body["sso_enforced_login_url"] is None


@pytest.mark.asyncio
async def test_enforced_sso_redirect_url_none_when_enabled_not_enforced(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSO enabled but NOT enforced keeps the password form — no auto-redirect."""
    await _clear_sso_connections()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # Self-hosted before the PUT (Enterprise-gated feature; self-hosted is entitled).
    monkeypatch.setattr(settings, "self_hosted", True)
    await _put_sso(client, "acme.example.com", enabled=True, enforced=False)

    body = (await client.get("/api/v1/config")).json()
    assert body["sso_enforced_login_url"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "multi", [pytest.param(True, id="multi-chat-on"), pytest.param(False, id="multi-chat-off")]
)
async def test_public_config_reports_whether_a_workspace_holds_several_chats(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    """The SPA groups chats under workspaces and offers "New workspace" only when
    a workspace may hold several chats, so the flag reaches it unauthenticated."""
    monkeypatch.setattr(settings, "workspaces_multi_chat", multi)
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200
    assert resp.json()["workspaces_multi_chat"] is multi


@pytest.mark.parametrize("enabled", [pytest.param(True, id="on"), pytest.param(False, id="off")])
@pytest.mark.asyncio
async def test_public_config_says_whether_one_sign_in_may_hold_several_orgs(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """The portal offers creating, leaving and joining another org only when
    the server says it runs with several orgs per person."""
    monkeypatch.setattr(settings, "multi_org_enabled", enabled)
    resp = await client.get("/api/v1/config")
    assert resp.status_code == 200
    assert resp.json()["multi_org_enabled"] is enabled
