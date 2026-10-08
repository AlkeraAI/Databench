"""An email domain belongs to one org, platform staff assign it, and a social
sign-in never walks into an account an org's IdP made.

Driven through the real routes: the staff route that assigns domains, the org
admin's SSO settings, sign-in discovery, the SSO callback, SCIM and the social
(mock) sign-in. Three rules, each with the attack it closes:

1. Only platform admins assign an org's domains; the org admin's settings
   cannot. Otherwise an org could claim another company's domain.
2. SCIM and an org's IdP create or link accounts only in domains assigned to
   that org.
3. A social sign-in is never linked by email into an account SCIM or an IdP
   created. Otherwise the org that created ``bob@victim.com`` receives the real
   Bob the first time he signs in with Google, and keeps access through its IdP.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from alkera_core.auth import COOKIE_NAME, decode_oauth_state, sso_domains
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    OAuthIdentity,
    OrgAuditEvent,
    SsoConnection,
    SsoDomainClaim,
    User,
)
from backend.auth.oauth.mock import MockProvider
from backend.auth.oauth.profile import FederatedProfile
from backend.services.identity import sso as sso_service
from backend.services.org import teams as team_service
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from tests.conftest import OrgWithAdmin, app_client, login, signed_in_through_sso

pytestmark = pytest.mark.asyncio

SCIM = "/api/v1/scim/v2"


@pytest.fixture(autouse=True)
def _sso_feature_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO is an Enterprise feature; run on a self-hosted deployment where every
    org has it. The SaaS gate is covered in test_enterprise_gating.py."""
    monkeypatch.setattr(settings, "self_hosted", True)


def _domain(label: str = "acme") -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}.example.com"


async def _org() -> OrgWithAdmin:
    email = f"admin-{uuid.uuid4().hex[:8]}@alkera.dev"
    password = "admin-pass-12345"
    async with AsyncSessionLocal() as s:
        org, admin = await team_service.create_org_with_admin(
            s,
            org_name=f"claims-org-{uuid.uuid4().hex[:8]}",
            admin_email=email,
            admin_first_name="Claims",
            admin_last_name="Admin",
            admin_password=password,
        )
        admin.email_verified_at = datetime.now(UTC)
        await s.commit()
        return OrgWithAdmin(
            org_id=org.id, admin_id=admin.id, admin_email=email, admin_password=password
        )


async def _as(person: OrgWithAdmin) -> AsyncClient:
    client = app_client()
    await login(client, person.admin_email, person.admin_password)
    return client


async def _assign(staff: OrgWithAdmin, org: uuid.UUID, *domains: str) -> Response:
    async with await _as(staff) as c:
        return await c.put(f"/admin/v1/orgs/{org}/sso/domains", json={"domains": list(domains)})


async def _configure(org: OrgWithAdmin, **extra: Any) -> Response:
    """The org admin's own SSO settings save."""
    async with await _as(org) as c:
        return await c.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.acme.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "s",
                "enabled": True,
                **extra,
            },
        )


async def _discover(email: str) -> dict[str, Any]:
    async with app_client() as c:
        resp = await c.get("/api/v1/auth/sso/discover", params={"email": email})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _holders(domain: str) -> list[uuid.UUID]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(SsoDomainClaim.org_team_id).where(SsoDomainClaim.domain == domain)
        )
        return list(rows.scalars().all())


async def _audit(org_id: uuid.UUID, *actions: str) -> list[tuple[str, str | None, str]]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(OrgAuditEvent.action, OrgAuditEvent.target, OrgAuditEvent.actor_email)
            .where(OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action.in_(actions))
            .order_by(OrgAuditEvent.created_at, OrgAuditEvent.id)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def _users(email: str) -> int:
    async with AsyncSessionLocal() as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(User).where(func.lower(User.email) == email)
            )
            or 0
        )


# --------------------------------------------------------------------------- #
# Rule 1: only platform admins assign domains
# --------------------------------------------------------------------------- #


async def test_a_platform_admin_assigns_domains_and_the_org_sees_them_read_only(
    platform_admin: OrgWithAdmin,
) -> None:
    domain, second = _domain(), _domain("second")
    org = await _org()
    resp = await _assign(platform_admin, org.org_id, f" {domain.upper()}. ", second, domain)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"org_id": str(org.org_id), "domains": sorted([domain, second])}

    async with await _as(org) as c:
        read = (await c.get("/api/v1/org/sso")).json()
    assert read["allowed_domains"] == ",".join(sorted([domain, second]))
    # Recorded on the org's own trail, naming the staff member who did it.
    assert sorted(await _audit(org.org_id, "sso.domain_assigned")) == sorted(
        [
            ("sso.domain_assigned", domain, platform_admin.admin_email),
            ("sso.domain_assigned", second, platform_admin.admin_email),
        ]
    )

    # Replacing the set removes what is left out, and says so.
    assert (await _assign(platform_admin, org.org_id, second)).status_code == 200
    assert await _holders(domain) == []
    assert await _audit(org.org_id, "sso.domain_removed") == [
        ("sso.domain_removed", domain, platform_admin.admin_email)
    ]


async def test_the_org_admins_own_settings_cannot_claim_a_domain() -> None:
    """The reported attack's first step: an org typing another company's
    domain into its SSO settings. The save goes through for everything else;
    the domain is not taken, routes nobody, and the org holds nothing."""
    victim = _domain("victim")
    attacker = await _org()
    resp = await _configure(attacker, allowed_domains=victim)
    assert resp.status_code == 200, resp.text
    assert resp.json()["allowed_domains"] == ""
    assert await _holders(victim) == []
    assert (await _discover(f"bob@{victim}"))["sso"] is False


@pytest.mark.parametrize("who", ["org_admin", "platform_support"])
async def test_only_a_platform_admin_may_change_an_orgs_domains(
    who: str, platform_support: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    org = await _org()
    caller = org if who == "org_admin" else platform_support
    resp = await _assign(caller, org.org_id, domain)
    assert resp.status_code == 403, resp.text
    assert await _holders(domain) == []
    # Support may read where the org stands; an org admin may not use the
    # platform surface at all.
    async with await _as(caller) as c:
        read = await c.get(f"/admin/v1/orgs/{org.org_id}/sso/domains")
    assert read.status_code == (403 if who == "org_admin" else 200)


# --------------------------------------------------------------------------- #
# One org per domain
# --------------------------------------------------------------------------- #


async def test_a_domain_another_org_holds_is_refused_and_stays_where_it_is(
    platform_admin: OrgWithAdmin,
) -> None:
    domain, own = _domain(), _domain("own")
    owner, second = await _org(), await _org()
    assert (await _assign(platform_admin, owner.org_id, domain)).status_code == 200
    assert (await _assign(platform_admin, second.org_id, own)).status_code == 200
    assert (await _configure(owner)).status_code == 200

    refused = await _assign(platform_admin, second.org_id, own, domain)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "sso_domain_held"
    assert error["message"] == f"{domain} is already assigned to another organization."
    assert str(owner.org_id) not in refused.text
    # All or nothing: the second org keeps exactly what it had.
    assert await _holders(domain) == [owner.org_id]
    assert await _holders(own) == [second.org_id]

    found = await _discover(f"jane@{domain}")
    assert found["sso"] is True
    assert str(owner.org_id) in found["login_url"]


async def test_the_loser_of_a_race_for_a_domain_gets_the_same_refusal(
    platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two assignments of one domain at once both pass the check that reads
    who holds it. The second is refused by the unique constraint, and must be
    told the same thing as a request that came a moment later, not a 500."""
    domain, own = _domain(), _domain("own")
    owner, second = await _org(), await _org()
    assert (await _assign(platform_admin, owner.org_id, domain)).status_code == 200
    assert (await _assign(platform_admin, second.org_id, own)).status_code == 200

    async def nobody_yet(db: object, domain: str) -> None:
        return None

    # The check as the loser saw it: the winner had not committed yet.
    with monkeypatch.context() as blind:
        blind.setattr(sso_domains, "owner_of", nobody_yet)
        refused = await _assign(platform_admin, second.org_id, own, domain)

    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "sso_domain_held"
    assert error["message"] == "One of these domains is already assigned to another organization."
    assert error["details"] == {"field": "domains"}
    assert str(owner.org_id) not in refused.text
    assert "uq_sso_domain_claims_domain" not in refused.text
    assert await _holders(domain) == [owner.org_id]
    assert await _holders(own) == [second.org_id]


async def test_the_database_holds_a_domain_for_one_org_only(platform_admin: OrgWithAdmin) -> None:
    domain = _domain()
    owner, second = await _org(), await _org()
    assert (await _assign(platform_admin, owner.org_id, domain)).status_code == 200
    async with AsyncSessionLocal() as s:
        s.add(SsoDomainClaim(org_team_id=second.org_id, domain=domain))
        with pytest.raises(IntegrityError, match="uq_sso_domain_claims_domain"):
            await s.flush()


@pytest.mark.parametrize(
    ("value", "says"),
    [
        pytest.param("gmail.com", "public mailbox domain", id="gmail"),
        pytest.param("Outlook.com", "public mailbox domain", id="outlook-any-case"),
        pytest.param("proton.me", "public mailbox domain", id="newer-provider"),
        pytest.param("acme", "Not an email domain", id="one-label"),
        pytest.param("*.acme.example.com", "Not an email domain", id="wildcard"),
        pytest.param("https://acme.example.com", "Not an email domain", id="url"),
    ],
)
async def test_a_value_no_org_may_hold_is_refused(
    platform_admin: OrgWithAdmin, value: str, says: str
) -> None:
    org = await _org()
    kept = _domain("kept")
    assert (await _assign(platform_admin, org.org_id, kept)).status_code == 200
    resp = await _assign(platform_admin, org.org_id, kept, value)
    assert resp.status_code == 400, resp.text
    assert says in resp.text
    async with await _as(platform_admin) as c:
        read = (await c.get(f"/admin/v1/orgs/{org.org_id}/sso/domains")).json()
    assert read["domains"] == [kept]


async def test_removing_the_last_domain_stops_requiring_sso(
    platform_admin: OrgWithAdmin,
) -> None:
    domain = _domain()
    org = await _org()
    assert (await _assign(platform_admin, org.org_id, domain)).status_code == 200
    assert (await _configure(org)).status_code == 200
    async with await _as(org) as c:
        await signed_in_through_sso(c, org.org_id)
        enforced = await c.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.acme.example.com",
                "oidc_client_id": "cid",
                "enabled": True,
                "enforced": True,
            },
        )
    assert enforced.status_code == 200, enforced.text
    assert enforced.json()["enforced"] is True

    assert (await _assign(platform_admin, org.org_id)).status_code == 200
    async with AsyncSessionLocal() as s:
        conn = await s.scalar(select(SsoConnection).where(SsoConnection.org_team_id == org.org_id))
    assert conn is not None
    assert conn.enforced is False
    assert await _audit(org.org_id, "sso.enforcement_disabled") == [
        ("sso.enforcement_disabled", None, platform_admin.admin_email)
    ]


# --------------------------------------------------------------------------- #
# Rule 2: SCIM and the IdP act only in the org's assigned domains
# --------------------------------------------------------------------------- #


async def _scim_token(org: OrgWithAdmin) -> str:
    async with await _as(org) as c:
        minted = await c.post("/api/v1/org/sso/scim-token")
    assert minted.status_code == 200, minted.text
    token: str = minted.json()["token"]
    return token


async def _scim_create(token: str, email: str) -> Response:
    async with app_client() as c:
        return await c.post(
            f"{SCIM}/Users",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": email,
                "name": {"givenName": "New", "familyName": "Hire"},
                "active": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )


async def test_scim_provisions_only_in_the_orgs_assigned_domains(
    platform_admin: OrgWithAdmin,
) -> None:
    """The reported squat: an org with a working SCIM token creates another
    company's addresses. Typing the domain into its own settings assigns
    nothing, so SCIM refuses, and the address stays free for its owner."""
    own, victim, other_org_domain = _domain("own"), _domain("victim"), _domain("theirs")
    attacker, other = await _org(), await _org()
    assert (await _assign(platform_admin, attacker.org_id, own)).status_code == 200
    assert (await _assign(platform_admin, other.org_id, other_org_domain)).status_code == 200
    assert (await _configure(attacker, allowed_domains=f"{own},{victim}")).status_code == 200
    token = await _scim_token(attacker)

    for email in (f"bob@{victim}", f"ceo@{other_org_domain}"):
        refused = await _scim_create(token, email)
        assert refused.status_code == 400, refused.text
        assert refused.json()["scimType"] == "invalidValue"
        assert await _users(email) == 0

    created = await _scim_create(token, f"mine@{own}")
    assert created.status_code == 201, created.text
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == f"mine@{own}"))).scalar_one()
    assert user.provisioned_by == "scim"


class _FakeIdp:
    """The org's IdP at its network edge: hands back the profile the test
    scripted for the code it is given. Everything past the edge is real."""

    def __init__(self, key: str, profiles: dict[str, FederatedProfile]) -> None:
        self.key = key
        self._profiles = profiles

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str) -> str:
        return f"https://idp.example.com/authorize?state={state}"

    async def fetch_profile(self, *, code: str, redirect_uri: str, nonce: str) -> FederatedProfile:
        return self._profiles[code]


async def _sso_sign_in(monkeypatch: pytest.MonkeyPatch, org_id: uuid.UUID, email: str) -> Response:
    """The org's OIDC sign-in, start to callback, with its IdP asserting ``email``.
    An IdP names one person by one subject, so the subject follows the address."""
    profiles: dict[str, FederatedProfile] = {}
    monkeypatch.setattr(
        sso_service,
        "build_oidc_provider",
        lambda conn: _FakeIdp(sso_service.provider_key(conn.org_team_id), profiles),
    )
    async with app_client() as browser:
        start = await browser.get(f"/api/v1/auth/sso/{org_id}/login", follow_redirects=False)
        assert start.status_code == 302, start.text
        state = decode_oauth_state(browser.cookies["alkera_oauth_tx"]).state
        code = f"code-{uuid.uuid4().hex}"
        profiles[code] = FederatedProfile(
            provider=sso_service.provider_key(org_id),
            subject=f"sub-{email}",
            email=email,
            email_verified=True,
            first_name="Idp",
            last_name="Asserted",
        )
        return await browser.get(
            f"/api/v1/auth/sso/{org_id}/login/callback",
            params={"code": code, "state": state},
            follow_redirects=False,
        )


async def test_an_idp_cannot_create_an_account_outside_its_orgs_domains(
    platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct link to the org's sign-in skips discovery, so the callback is
    held to the same rule: the IdP asserts only addresses in the org's
    assigned domains."""
    own, victim = _domain("own"), _domain("victim")
    org = await _org()
    assert (await _assign(platform_admin, org.org_id, own)).status_code == 200
    assert (await _configure(org, allowed_domains=victim)).status_code == 200

    refused = await _sso_sign_in(monkeypatch, org.org_id, f"bob@{victim}")
    assert "oauth_error=sso_domain_mismatch" in refused.headers["location"]
    assert await _users(f"bob@{victim}") == 0

    allowed = await _sso_sign_in(monkeypatch, org.org_id, f"bob@{own}")
    assert "oauth_error" not in allowed.headers["location"]
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == f"bob@{own}"))).scalar_one()
    assert user.home_org_team_id == org.org_id
    assert user.provisioned_by == "sso"


# --------------------------------------------------------------------------- #
# Rule 3: a social sign-in never walks into a provisioned account
# --------------------------------------------------------------------------- #


async def _google_sign_in(email: str) -> tuple[Response, bool]:
    """A social sign-in (the mock provider, which reports the address as
    verified, as Google does), in its own cookie jar."""
    async with app_client() as c:
        start = await c.get(
            "/api/v1/auth/oauth/mock/start",
            params={"intent": "login", "return_to": "/dashboard"},
            follow_redirects=False,
        )
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        code = MockProvider.encode_code(
            FederatedProfile(
                provider="mock",
                subject=f"google-{secrets.token_hex(8)}",
                email=email,
                email_verified=True,
                first_name="Real",
                last_name="Bob",
            )
        )
        resp = await c.get(
            "/api/v1/auth/oauth/mock/callback",
            params={"state": state, "code": code},
            follow_redirects=False,
        )
        return resp, COOKIE_NAME in c.cookies


async def _social_links(email: str) -> int:
    async with AsyncSessionLocal() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(OAuthIdentity)
                .join(User, User.id == OAuthIdentity.user_id)
                .where(User.email == email, OAuthIdentity.provider == "mock")
            )
            or 0
        )


@pytest.mark.parametrize("made_by", ["scim", "idp"])
async def test_the_real_person_is_never_signed_into_an_account_an_org_made(
    platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, made_by: str
) -> None:
    """The attack end to end. An org was given a domain (here by staff, the
    only way left), and its SCIM or IdP created ``bob@<domain>`` with the
    address marked verified. The person who really owns that mailbox signs in
    with Google: they must not land in that org's account, and nothing may be
    linked that would let them, or let the org's IdP keep reaching them."""
    domain = _domain("victim")
    org = await _org()
    assert (await _assign(platform_admin, org.org_id, domain)).status_code == 200
    assert (await _configure(org)).status_code == 200
    email = f"bob-{uuid.uuid4().hex[:6]}@{domain}"
    if made_by == "scim":
        assert (await _scim_create(await _scim_token(org), email)).status_code == 201
    else:
        made = await _sso_sign_in(monkeypatch, org.org_id, email)
        assert "oauth_error" not in made.headers["location"]

    resp, signed_in = await _google_sign_in(email)
    assert "oauth_error=sso_sign_in_required" in resp.headers["location"]
    assert not signed_in
    assert await _social_links(email) == 0

    # The account still opens the way the org set it up.
    through_idp = await _sso_sign_in(monkeypatch, org.org_id, email)
    assert "oauth_error" not in through_idp.headers["location"]


async def test_an_account_the_person_made_still_links_a_social_sign_in() -> None:
    """The other side of the rule, so it cannot pass by refusing everyone:
    an account its owner created keeps the usual link by verified email."""
    org = await _org()
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == org.admin_id).values(provisioned_by=None))
        await s.commit()
    resp, signed_in = await _google_sign_in(org.admin_email)
    assert "oauth_error" not in resp.headers["location"], resp.headers["location"]
    assert signed_in
    assert await _social_links(org.admin_email) == 1
