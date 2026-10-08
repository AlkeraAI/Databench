"""Platform-staff entitlement minting: the issuance ledger on the self-hosted
orgs console. Minting is ALKERA_ADMIN-only, requires the signing seed, and must
look nonexistent on a customer install."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alkera_core import entitlements as ent
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.entitlements import generate_keypair, parse_entitlement_token
from alkera_core.models import EntitlementGrant
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login

PRIV, PUB = generate_keypair()


@pytest.fixture(autouse=True)
def _minting_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: an Alkera-SaaS-shaped deployment with the signing seed set."""
    monkeypatch.setattr(settings, "self_hosted", False)
    monkeypatch.setattr(settings, "alkera_entitlements_signing_key", PRIV)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", None)
    ent.get_entitlements.cache_clear()


async def _customer_org() -> UUID:
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        org, _admin = await team_service.create_org_with_admin(
            s,
            org_name=f"Customer {secrets.token_hex(4)}",
            admin_email=f"cust-{secrets.token_hex(5)}@customer.example",
            admin_first_name="Cust",
            admin_last_name="Admin",
            admin_password="customer-pass-12345",
        )
        await s.commit()
        return org.id


def _mint_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "customer_slug": "acme-corp",
        "features": ["byok"],
        "expires_on": (datetime.now(UTC) + timedelta(days=365)).date().isoformat(),
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_non_staff_cannot_reach_entitlements(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    org_id = await _customer_org()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"/admin/v1/enterprise-orgs/{org_id}/entitlements")).status_code == 403


@pytest.mark.asyncio
async def test_support_can_list_but_not_mint(
    client: AsyncClient, platform_support: OrgWithAdmin
) -> None:
    org_id = await _customer_org()
    await login(client, platform_support.admin_email, platform_support.admin_password)
    assert (await client.get(f"/admin/v1/enterprise-orgs/{org_id}/entitlements")).json() == []
    minted = await client.post(
        f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body()
    )
    assert minted.status_code == 403  # ALKERA_ADMIN only — this hands out a billing switch


@pytest.mark.asyncio
async def test_mint_round_trip_serial_and_supersession(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    org_id = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    first = await client.post(f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body())
    assert first.status_code == 201
    body = first.json()
    assert body["customer_slug"] == "acme-corp"
    assert body["features"] == ["byok"]
    assert body["superseded_at"] is None

    # The returned token is a genuine grant: it verifies against the pubkey and
    # embeds the DB-assigned serial + the requested payload.
    parsed = parse_entitlement_token(body["token"], public_keys={"alk1": PUB})
    assert parsed.customer == "acme-corp"
    assert parsed.serial == body["serial"]
    assert parsed.feature_names() == ["byok"]

    second = (
        await client.post(f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body())
    ).json()
    assert second["serial"] > body["serial"]  # DB identity strictly increases

    listing = (await client.get(f"/admin/v1/enterprise-orgs/{org_id}/entitlements")).json()
    assert [g["serial"] for g in listing] == [second["serial"], body["serial"]]  # newest first
    by_id = {g["id"]: g for g in listing}
    assert by_id[body["id"]]["superseded_at"] is not None  # auto-superseded by the re-mint
    assert by_id[second["id"]]["superseded_at"] is None

    # The full token is stored on the row (re-displayable by design).
    async with AsyncSessionLocal() as s:
        row = await s.get(EntitlementGrant, UUID(body["id"]))
        assert row is not None
        assert row.token == body["token"]


@pytest.mark.asyncio
async def test_mint_rejects_past_expiry_and_unknown_org(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    org_id = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    past = await client.post(
        f"/admin/v1/enterprise-orgs/{org_id}/entitlements",
        json=_mint_body(expires_on="2020-01-01"),
    )
    assert past.status_code == 400
    missing = await client.post(
        f"/admin/v1/enterprise-orgs/{uuid4()}/entitlements", json=_mint_body()
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_mint_surface_hidden_on_self_hosted(
    client: AsyncClient, platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A customer install (their own platform admins!) must see a 404, not a
    disabled-but-present minting surface."""
    org_id = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    monkeypatch.setattr(settings, "self_hosted", True)
    resp = await client.post(f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_503_when_signing_key_unset(
    client: AsyncClient, platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Alkera's SaaS a missing seed is a misconfiguration — loud, not hidden."""
    org_id = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    monkeypatch.setattr(settings, "alkera_entitlements_signing_key", None)
    resp = await client.post(f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body())
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_supersede_is_idempotent_and_org_scoped(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    org_a = await _customer_org()
    org_b = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    grant = (
        await client.post(f"/admin/v1/enterprise-orgs/{org_a}/entitlements", json=_mint_body())
    ).json()

    first = await client.post(
        f"/admin/v1/enterprise-orgs/{org_a}/entitlements/{grant['id']}/supersede"
    )
    assert first.status_code == 204
    again = await client.post(
        f"/admin/v1/enterprise-orgs/{org_a}/entitlements/{grant['id']}/supersede"
    )
    assert again.status_code == 204  # idempotent

    # A grant reached through the WRONG org 404s (no cross-org probing).
    foreign = await client.post(
        f"/admin/v1/enterprise-orgs/{org_b}/entitlements/{grant['id']}/supersede"
    )
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_mint_is_audited_without_leaking_the_token(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    from alkera_core.models import AuditLog

    org_id = await _customer_org()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    minted = (
        await client.post(f"/admin/v1/enterprise-orgs/{org_id}/entitlements", json=_mint_body())
    ).json()

    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.path == f"/admin/v1/enterprise-orgs/{org_id}/entitlements"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "mint must land in audit_logs (AuditedRoute)"
    serialized = "".join(str(r.detail) for r in rows)
    assert minted["token"] not in serialized  # the signed token never enters the audit log
