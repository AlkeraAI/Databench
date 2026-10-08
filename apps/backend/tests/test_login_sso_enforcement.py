"""Which accounts an enforced SSO connection may refuse a password login for.

`allowed_domains` is an UNVERIFIED claim — any Enterprise tenant can type any
domain into it — so the enforcement decision at `POST /api/v1/auth/login` must be
resolved from the caller's OWN org, never from whichever connection on the
deployment happens to claim their email's domain. These tests pin the tenant
boundary in both directions: a foreign claim must not lock anyone out, and the
account's own org's claim must not be escapable by editing the email column.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OAuthIdentity, User
from backend.services.identity import sso as sso_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import hold_sso_domains, make_member

pytestmark = pytest.mark.asyncio


async def _org() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        _team, admin = await team_service.create_org_with_admin(
            s,
            org_name=f"sso-org-{uuid.uuid4().hex[:8]}",
            admin_email=f"admin-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="A",
            admin_last_name="D",
            admin_password="admin-pass-12345",
        )
        await s.commit()
        return admin.home_org_team_id


async def _member(org_id: uuid.UUID, *, domain: str) -> tuple[User, str]:
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(
            s,
            org_id=org_id,
            email=f"u-{uuid.uuid4().hex[:8]}@{domain}",
            verified=True,
        )
        await s.commit()
    assert pw is not None
    return member, pw


async def _connect_sso(
    org_id: uuid.UUID, *, domains: str, enabled: bool = True, enforced: bool = True
) -> None:
    """Give ``org_id`` an OIDC connection claiming ``domains``. Written through
    the real service (the route adds only the Enterprise/admin gate, which is
    covered in test_sso.py) — the point here is what LOGIN does with the row."""
    async with AsyncSessionLocal() as s:
        await sso_service.upsert_oidc_connection(
            s,
            org_team_id=org_id,
            issuer="https://idp.example.com",
            client_id="cid",
            client_secret="shh",
            enabled=enabled,
            enforced=enforced,
        )
        await s.commit()
    await hold_sso_domains(org_id, domains)


async def _set_email(user_id: uuid.UUID, email: str) -> None:
    """Rename the account the way `PATCH /api/v1/users/{self}` does."""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        row.email = email
        await s.commit()


async def _link_sso_identity(user: User, org_id: uuid.UUID) -> None:
    """The row `oauth_service` writes when this org's IdP provisions/logs in a user."""
    async with AsyncSessionLocal() as s:
        s.add(
            OAuthIdentity(
                user_id=user.id,
                provider=sso_service.provider_key(org_id),
                subject=uuid.uuid4().hex,
                email_at_link=user.email,
                email_verified=True,
            )
        )
        await s.commit()


async def _login(client: AsyncClient, email: str, password: str):  # type: ignore[no-untyped-def]
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_another_orgs_enforced_connection_cannot_block_a_login(
    client: AsyncClient,
) -> None:
    """A domain claim is not ownership. An org that claims someone else's email
    domain must not be able to refuse that domain's users everywhere else."""
    domain = f"victim-{uuid.uuid4().hex[:8]}.example.com"
    victim_org = await _org()
    victim, pw = await _member(victim_org, domain=domain)

    squatter_org = await _org()
    await _connect_sso(squatter_org, domains=domain)

    resp = await _login(client, victim.email, pw)
    assert resp.status_code == 200, resp.text


async def test_a_bulk_domain_claim_cannot_lock_out_other_tenants(client: AsyncClient) -> None:
    """The scaled version of the same claim: `allowed_domains` is a free-text
    list, so one tenant can enumerate every domain it wants — including the
    public mailbox providers — in a single request. None of it may reach another
    tenant's users. (Stand-in domains here so the assertion doesn't depend on
    which addresses the rest of the suite happens to use.)"""
    victim_org = await _org()
    victim_domain = f"victim-{uuid.uuid4().hex[:8]}.example.com"
    victim, pw = await _member(victim_org, domain=victim_domain)

    squatter_org = await _org()
    await _connect_sso(
        squatter_org,
        domains=",".join(
            [
                f"webmail-{uuid.uuid4().hex[:8]}.example.com",
                f"inbox-{uuid.uuid4().hex[:8]}.example.com",
                victim_domain,
            ]
        ),
    )

    assert (await _login(client, victim.email, pw)).status_code == 200


async def test_own_orgs_enforced_connection_refuses_password_login(client: AsyncClient) -> None:
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    org = await _org()
    member, pw = await _member(org, domain=domain)
    await _connect_sso(org, domains=domain)

    resp = await _login(client, member.email, pw)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sso_required"


@pytest.mark.parametrize(
    ("enabled", "enforced"),
    [
        pytest.param(True, False, id="enabled-not-enforced"),
        pytest.param(False, False, id="disabled"),
    ],
)
async def test_a_connection_that_does_not_enforce_leaves_password_login_alone(
    client: AsyncClient, enabled: bool, enforced: bool
) -> None:
    domain = f"soft-{uuid.uuid4().hex[:8]}.example.com"
    org = await _org()
    member, pw = await _member(org, domain=domain)
    await _connect_sso(org, domains=domain, enabled=enabled, enforced=enforced)

    assert (await _login(client, member.email, pw)).status_code == 200


async def test_renaming_out_of_the_claimed_domain_does_not_escape_enforcement(
    client: AsyncClient,
) -> None:
    """An IdP-provisioned account carries its own binding to the org's connection,
    so moving the email column out of `allowed_domains` (self-service, no admin,
    no audit) must not drop it back to password login."""
    domain = f"bound-{uuid.uuid4().hex[:8]}.example.com"
    org = await _org()
    member, pw = await _member(org, domain=domain)
    await _link_sso_identity(member, org)
    await _connect_sso(org, domains=domain)

    # A domain no connection anywhere claims — so the ONLY thing that can still
    # refuse this login is the account's own binding to its org's IdP.
    outside = f"escapee-{uuid.uuid4().hex[:8]}@personal-{uuid.uuid4().hex[:8]}.example.com"
    await _set_email(member.id, outside)

    resp = await _login(client, outside, pw)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sso_required"


async def test_a_member_outside_the_claimed_domain_is_not_locked_out(
    client: AsyncClient,
) -> None:
    """The asymmetric case: a contractor in an SSO-enforcing org whose address the
    connection never claimed, and who has no federated identity, still has a
    password as their only way in — enforcement must not sweep them up."""
    org = await _org()
    contractor, pw = await _member(org, domain=f"contractor-{uuid.uuid4().hex[:8]}.example.com")
    await _connect_sso(org, domains=f"corp-{uuid.uuid4().hex[:8]}.example.com")

    assert (await _login(client, contractor.email, pw)).status_code == 200


async def test_a_wrong_password_is_still_a_plain_401_under_enforcement(
    client: AsyncClient,
) -> None:
    """Enforcement must not become a policy oracle: bad credentials look the same
    whether or not the org enforces SSO."""
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    org = await _org()
    member, _pw = await _member(org, domain=domain)
    await _connect_sso(org, domains=domain)

    assert (await _login(client, member.email, "not-the-password")).status_code == 401
