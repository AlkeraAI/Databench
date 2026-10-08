"""Who may hand out a proxy token.

A proxy token authenticates to the model gateway AS an org and everything it
spends is invoiced to that org, and nothing tenant-facing lets the customer see
or kill one. That puts minting in the same class as enrolling an org or minting
an entitlement — ALKERA_ADMIN — rather than at the ALKERA_SUPPORT floor the rest
of the console sits on. Revocation deliberately stays at that floor: it takes a
credential away.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ProxyToken
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio


def _tokens_url(org_id: UUID) -> str:
    return f"/admin/v1/enterprise-orgs/{org_id}/tokens"


async def _token_rows(org_id: UUID) -> list[ProxyToken]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(ProxyToken).where(ProxyToken.org_team_id == org_id))
        return list(rows.scalars().all())


async def _mint_as_admin(client: AsyncClient, admin: OrgWithAdmin, org_id: UUID) -> str:
    await login(client, admin.admin_email, admin.admin_password)
    resp = await client.post(_tokens_url(org_id), json={"label": "vpc"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"]["id"])


async def test_support_staff_cannot_mint_a_proxy_token(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """The escalation that matters: a support account (or anything that
    compromises one) must not be able to issue a spending credential against a
    customer's org."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(_tokens_url(org_admin.org_id), json={"label": "vpc"})
    assert resp.status_code == 403
    # Refused before anything was written — no secret, no row.
    assert await _token_rows(org_admin.org_id) == []


async def test_support_staff_cannot_rotate_a_proxy_token(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
) -> None:
    """A rotation returns a brand-new working secret, so it is a mint under
    another name and cannot be the way around the bar."""
    token_id = await _mint_as_admin(client, platform_admin, org_admin.org_id)

    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.post(f"{_tokens_url(org_admin.org_id)}/{token_id}/rotate")
    assert resp.status_code == 403
    assert len(await _token_rows(org_admin.org_id)) == 1


async def test_a_platform_admin_mints_and_rotates(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    """The bar is a bar, not a wall: the role that is supposed to issue these
    still can, and rotation still leaves the old secret live for zero-downtime
    cutover."""
    token_id = await _mint_as_admin(client, platform_admin, org_admin.org_id)

    rotated = await client.post(f"{_tokens_url(org_admin.org_id)}/{token_id}/rotate")
    assert rotated.status_code == 201
    assert rotated.json()["secret"].startswith("alk_proxy_")
    assert rotated.json()["token"]["label"] == "vpc"
    rows = {str(t.id): t for t in await _token_rows(org_admin.org_id)}
    assert len(rows) == 2
    assert rows[token_id].revoked_at is None


async def test_support_staff_can_still_revoke_a_token(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
) -> None:
    """The deliberate asymmetry. Whoever notices an abused credential has to be
    able to stop it without finding an admin first, so revoke stays at the
    support floor even though minting no longer is."""
    token_id = await _mint_as_admin(client, platform_admin, org_admin.org_id)

    await login(client, platform_support.admin_email, platform_support.admin_password)
    assert (await client.delete(f"{_tokens_url(org_admin.org_id)}/{token_id}")).status_code == 204
    assert (await client.delete(f"{_tokens_url(org_admin.org_id)}/{uuid4()}")).status_code == 404
    rows = {str(t.id): t for t in await _token_rows(org_admin.org_id)}
    assert rows[token_id].revoked_at is not None


async def test_a_tenant_admin_cannot_reach_the_mint_at_all(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The router floor is unchanged by the escalation: an ordinary customer org
    admin, with no platform role, never gets in."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(_tokens_url(org_admin.org_id), json={"label": "mine"})
    assert resp.status_code == 403
    assert await _token_rows(org_admin.org_id) == []
