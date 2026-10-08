"""Enterprise SSO: the cross-org takeover gate (the #1 security test), JIT
provisioning, the domain gate, and the org-admin config surface.

The gate lives in ``oauth_service.resolve`` and is exercised directly with a
constructed ``FederatedProfile`` + ``IdpScope`` — no network / real IdP — because
that is exactly the trust boundary an enterprise security review probes: a per-org
IdP must never be able to assert an identity that belongs to another org.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import httpx
import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OAuthIdentity, TeamMembership
from backend.auth.oauth import FederatedProfile
from backend.services.identity import oauth as oauth_service
from backend.services.identity import sso as sso_service
from backend.services.identity.oauth import IdpScope, LoginOutcome, OAuthLoginBlockedError
from httpx import AsyncClient, Response
from sqlalchemy import select
from tests.conftest import (
    OrgWithAdmin,
    hold_sso_domains,
    login,
    make_member,
    make_org_enterprise,
    signed_in_through_sso,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _sso_feature_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO is an Enterprise feature; this suite exercises the feature itself, so run it
    on a self-hosted deployment where it's available. The SaaS Enterprise gate (a
    Contact-Sales 403 for a non-Enterprise org) is covered in test_enterprise_gating.py.
    A later setattr wins over the root conftest's SaaS-by-default pin."""
    monkeypatch.setattr(settings, "self_hosted", True)


async def _make_org() -> UUID:
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        _org, admin = await team_service.create_org_with_admin(
            s,
            org_name=f"org-{uuid.uuid4().hex[:8]}",
            admin_email=f"admin-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="A",
            admin_last_name="D",
            admin_password="pass-123456",
        )
        await s.commit()
        return admin.home_org_team_id


async def _unverified_admin() -> tuple[str, str]:
    """An org admin whose email is NOT yet verified (in the grace window) —
    create_org_with_admin does not stamp email_verified_at."""
    from backend.services.org import teams as team_service

    email = f"admin-{uuid.uuid4().hex[:8]}@alkera.dev"
    async with AsyncSessionLocal() as s:
        _org, _admin = await team_service.create_org_with_admin(
            s,
            org_name=f"org-{uuid.uuid4().hex[:8]}",
            admin_email=email,
            admin_first_name="A",
            admin_last_name="D",
            admin_password="pass-123456",
        )
        await s.commit()
    return email, "pass-123456"


async def _victim(org_id: UUID, email: str) -> UUID:
    async with AsyncSessionLocal() as s:
        user, _pw = await make_member(s, org_id=org_id, email=email, verified=True)
        return user.id


def _profile(*, org_id: UUID, email: str, subject: str | None = None) -> FederatedProfile:
    return FederatedProfile(
        provider=sso_service.provider_key(org_id),
        subject=subject or f"sub-{uuid.uuid4().hex}",
        email=email,
        email_verified=True,
        first_name="SSO",
        last_name="User",
    )


# --------------------------------------------------------------------------- #
# THE cross-org takeover gate
# --------------------------------------------------------------------------- #


async def test_cross_org_idp_cannot_take_over_another_orgs_user() -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    victim_email = f"victim@{domain}"
    org_a = await _make_org()
    await _victim(org_a, victim_email)  # the victim lives in org A
    org_b = await _make_org()  # the attacker controls org B's IdP

    # Org B's IdP asserts it owns `domain` AND vouches for victim@domain — but the
    # victim's account is in org A. The gate MUST refuse.
    scope_b = IdpScope(org_team_id=org_b, domains=frozenset({domain}))
    async with AsyncSessionLocal() as s:
        with pytest.raises(OAuthLoginBlockedError) as exc:
            await oauth_service.resolve(
                s, _profile(org_id=org_b, email=victim_email), invite_token=None, idp_scope=scope_b
            )
    assert exc.value.reason == "sso_org_mismatch"

    # ...and nothing was linked to the victim.
    async with AsyncSessionLocal() as s:
        links = (
            (
                await s.execute(
                    select(OAuthIdentity).where(OAuthIdentity.email_at_link == victim_email)
                )
            )
            .scalars()
            .all()
        )
        assert links == []


async def test_domain_outside_allowlist_is_refused() -> None:
    org_a = await _make_org()
    scope = IdpScope(org_team_id=org_a, domains=frozenset({"acme.example.com"}))
    async with AsyncSessionLocal() as s:
        with pytest.raises(OAuthLoginBlockedError) as exc:
            await oauth_service.resolve(
                s,
                _profile(org_id=org_a, email="someone@evil.example.com"),
                invite_token=None,
                idp_scope=scope,
            )
    assert exc.value.reason == "sso_domain_mismatch"


# --------------------------------------------------------------------------- #
# Legitimate same-org flows
# --------------------------------------------------------------------------- #


async def test_same_org_existing_user_links_and_logs_in() -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp@{domain}"
    org_a = await _make_org()
    user_id = await _victim(org_a, email)
    scope = IdpScope(org_team_id=org_a, domains=frozenset({domain}))
    async with AsyncSessionLocal() as s:
        outcome = await oauth_service.resolve(
            s, _profile(org_id=org_a, email=email), invite_token=None, idp_scope=scope
        )
        await s.commit()
    assert isinstance(outcome, LoginOutcome)
    assert outcome.user.id == user_id


async def test_jit_provisions_a_new_user_into_the_idp_org() -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    email = f"newhire@{domain}"
    org_a = await _make_org()
    scope = IdpScope(org_team_id=org_a, domains=frozenset({domain}))
    async with AsyncSessionLocal() as s:
        outcome = await oauth_service.resolve(
            s, _profile(org_id=org_a, email=email), invite_token=None, idp_scope=scope
        )
        await s.commit()
    assert isinstance(outcome, LoginOutcome)
    new_user = outcome.user
    assert new_user.home_org_team_id == org_a
    assert new_user.email == email
    assert new_user.email_verified_at is not None  # IdP-vouched
    # Provisioned as a member of the org.
    async with AsyncSessionLocal() as s:
        membership = (
            await s.execute(
                select(TeamMembership).where(
                    TeamMembership.user_id == new_user.id, TeamMembership.team_id == org_a
                )
            )
        ).scalar_one_or_none()
        assert membership is not None


# --------------------------------------------------------------------------- #
# Org-admin config surface
# --------------------------------------------------------------------------- #


async def test_config_requires_secret_on_first_set_and_never_returns_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)

    # First configure WITHOUT a secret → rejected.
    bad = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "enabled": False,
        },
    )
    assert bad.status_code == 400

    # With a secret → created; the secret is never echoed back.
    ok = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "shhh-secret",
            "enabled": True,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["has_client_secret"] is True
    assert "secret" not in str(body).lower() or "shhh-secret" not in str(body)
    assert "shhh-secret" not in str(body)

    # GET reflects config, still no secret.
    got = (await client.get("/api/v1/org/sso")).json()
    assert got["configured"] is True
    assert got["enabled"] is True
    assert got["oidc_issuer"] == "https://idp.acme.example.com"
    assert "shhh-secret" not in str(got)


async def _as_saas(monkeypatch: pytest.MonkeyPatch, org_id: UUID) -> None:
    """Flip the deployment to Alkera's hosted service, keeping the SSO surface
    reachable — it is Enterprise-gated there, and these cases are about the ISSUER
    rule, not the plan gate (that one is test_enterprise_gating.py)."""
    await make_org_enterprise(org_id)
    monkeypatch.setattr(settings, "self_hosted", False)


async def _put_issuer(client: AsyncClient, issuer: str) -> Response:
    return await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": issuer,
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
        },
    )


@pytest.mark.parametrize("self_hosted", [True, False], ids=["self-hosted", "saas"])
@pytest.mark.parametrize(
    "issuer",
    [
        pytest.param("http://idp.acme.example.com", id="http-downgrade"),
        # The suffix-absorbing tricks: `/.well-known/openid-configuration` would be
        # swallowed by a query/fragment, making discovery fetch an arbitrary path.
        pytest.param("https://idp.acme.example.com/?", id="query-absorbs-suffix"),
        pytest.param("https://idp.acme.example.com/#", id="fragment-absorbs-suffix"),
        pytest.param("https://user:pw@idp.acme.example.com", id="userinfo"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://[::1/idp", id="unparseable"),
        pytest.param("not-a-url", id="not-a-url"),
    ],
)
async def test_a_malformed_issuer_is_refused_on_every_deployment(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    issuer: str,
    self_hosted: bool,
) -> None:
    """Shape is not negotiable on either deployment: the issuer is dialed BY THE
    SERVER — discovery, then the token exchange carrying the org's client secret —
    so a downgraded scheme, embedded credentials, a hostless or unparseable URL, or
    a suffix-absorbing `?`/`#` is refused whoever is running the install. (Only
    REACHABILITY relaxes self-hosted; see the private-issuer case below.)"""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if self_hosted:
        monkeypatch.setattr(settings, "self_hosted", True)
    else:
        await _as_saas(monkeypatch, org_admin.org_id)
    resp = await _put_issuer(client, issuer)
    assert resp.status_code == 400, resp.text
    # Nothing was stored, so the public login route has nothing to dial.
    assert (await client.get("/api/v1/org/sso")).json()["configured"] is False


@pytest.mark.parametrize(
    "issuer",
    [
        pytest.param("https://10.0.3.17:8080/internal", id="rfc1918"),
        pytest.param("https://127.0.0.1/idp", id="loopback"),
        pytest.param("https://169.254.170.2/v2/credentials", id="link-local-metadata"),
        pytest.param("https://localhost/idp", id="localhost"),
        pytest.param("https://sso.corp.internal/realms/acme", id="internal-suffix"),
        pytest.param("https://intranet/idp", id="bare-hostname"),
    ],
)
async def test_a_private_issuer_is_refused_on_saas_and_configurable_self_hosted(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, issuer: str
) -> None:
    """Reachability is the one rule that depends on who runs the install.

    On Alkera's SaaS a tenant must never be able to point the backend at an address
    inside Alkera's own network, so a private issuer is refused. A customer's own
    install is the opposite case: its IdP (Keycloak, ADFS) normally lives on an
    internal address, its org admin already owns that network, and refusing would
    make SSO unconfigurable on the very deployment where it is always available.
    Both directions are pinned so the carve-out can't silently invert."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await _as_saas(monkeypatch, org_admin.org_id)
    refused = await _put_issuer(client, issuer)
    assert refused.status_code == 400, refused.text
    assert (await client.get("/api/v1/org/sso")).json()["configured"] is False

    monkeypatch.setattr(settings, "self_hosted", True)
    accepted = await _put_issuer(client, issuer)
    assert accepted.status_code == 200, accepted.text
    assert (await client.get("/api/v1/org/sso")).json()["oidc_issuer"] == issuer


async def test_a_public_https_issuer_with_a_path_is_accepted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: real IdPs put a tenant path on the issuer (Azure AD's
    `/{tenant}/v2.0`, Okta's `/oauth2/default`), so a PATH is fine — only a query,
    a fragment, a private host, or a non-https scheme are not."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://login.microsoftonline.com/tenant-id/v2.0",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text


async def test_a_private_issuer_stored_before_the_guard_is_never_dialed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed on data, not just on input: a row written on a self-hosted
    install (or by any other path) must not keep its reach once the deployment is
    SaaS.

    The assertion that matters is that NOTHING was dialed — a request that leaves
    and then fails has already touched the address, which is the whole point of the
    guard. The wire is scripted, so the refusal cannot be a network timeout."""
    from backend.auth.oauth import oidc
    from backend.auth.oauth.base import OAuthError
    from backend.auth.oauth.oidc import OidcProvider

    monkeypatch.setattr(settings, "self_hosted", False)  # SaaS: private is refused
    dialed: list[str] = []

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            dialed.append(str(request.url))
            return httpx.Response(200, json={"authorization_endpoint": "https://idp.test/auth"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(oidc, "async_client", factory)
    provider = OidcProvider(
        key="sso:test",
        # https + a well-formed host: the private ADDRESS is the only thing wrong,
        # so dropping the reachability rule would let this dial the metadata service.
        issuer="https://169.254.170.2/v2/credentials",
        client_id="cid",
        client_secret="s",
    )
    with pytest.raises(OAuthError):
        await provider.authorization_url(redirect_uri="https://app/cb", state="s", nonce="n")
    assert dialed == []


async def test_repointing_the_issuer_requires_re_entering_the_client_secret(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The stored client secret is POSTed to whatever endpoint the issuer's
    discovery document names. An org admin can never READ it — so they must not be
    able to have it DELIVERED to an IdP they control by moving the issuer with the
    secret field left blank (the same anti-exfiltration rule BYOK provider keys
    carry)."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    base = {
        "oidc_issuer": "https://idp.acme.example.com",
        "oidc_client_id": "cid",
        "enabled": True,
    }
    assert (
        await client.put("/api/v1/org/sso", json={**base, "oidc_client_secret": "real-secret"})
    ).status_code == 200

    # Move the issuer, omit the secret → refused, and nothing is stored.
    moved = await client.put(
        "/api/v1/org/sso", json={**base, "oidc_issuer": "https://evil.example"}
    )
    assert moved.status_code == 400
    assert "secret" in moved.text.lower()
    got = (await client.get("/api/v1/org/sso")).json()
    assert got["oidc_issuer"] == "https://idp.acme.example.com"

    # Same for the client id (it identifies the app the secret belongs to).
    swapped = await client.put("/api/v1/org/sso", json={**base, "oidc_client_id": "other-cid"})
    assert swapped.status_code == 400
    assert (await client.get("/api/v1/org/sso")).json()["oidc_client_id"] == "cid"

    # Editing anything else with the secret omitted still works (the keep path).
    assert (
        await client.put("/api/v1/org/sso", json={**base, "groups_mapping": {"admins": "admin"}})
    ).status_code == 200
    # ...and re-entering the secret authorizes the move.
    assert (
        await client.put(
            "/api/v1/org/sso",
            json={
                **base,
                "oidc_issuer": "https://new-idp.example.com",
                "oidc_client_secret": "re-entered",
            },
        )
    ).status_code == 200
    assert (await client.get("/api/v1/org/sso")).json()[
        "oidc_issuer"
    ] == "https://new-idp.example.com"


async def test_switching_to_saml_drops_the_orphaned_oidc_client_secret(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A secret no protocol uses is a secret that can still be re-pointed later."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (
        await client.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.acme.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "real-secret",
                "enabled": True,
            },
        )
    ).status_code == 200

    saml = await client.put(
        "/api/v1/org/sso",
        json={
            "protocol": "saml",
            "saml_idp_entity_id": "https://idp.acme.example.com/metadata",
            "saml_sso_url": "https://idp.acme.example.com/sso",
            "saml_x509_cert": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "enabled": True,
        },
    )
    assert saml.status_code == 200, saml.text
    assert saml.json()["has_client_secret"] is False
    # Going back to OIDC therefore demands the secret again.
    back = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "enabled": True,
        },
    )
    assert back.status_code == 400


async def test_config_denied_for_non_admin(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as s:
        user, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login(client, user.email, pw)
    assert (await client.get("/api/v1/org/sso")).status_code == 403


async def test_unverified_org_admin_cannot_mutate_security_config(client: AsyncClient) -> None:
    """The grace-window squat defense: an org admin whose email is NOT yet proven
    can READ but cannot CONFIGURE security (SSO), mint proxy tokens, or issue
    credit — those require a verified email (OrgAdminVerified)."""
    email, pw = await _unverified_admin()
    await login(client, email, pw)

    # Reads stay available (org-admin, not verification-gated).
    assert (await client.get("/api/v1/org/sso")).status_code == 200

    # ...but every top-level security/money MUTATION is refused until verified.
    sso = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
        },
    )
    assert sso.status_code == 403
    assert sso.json()["error"]["code"] == "email_verification_required"

    fund = await client.post("/api/v1/org/billing/pool/grants", json={"amount_usd": "10.00"})
    assert fund.status_code == 403


# --------------------------------------------------------------------------- #
# Domain discovery
# --------------------------------------------------------------------------- #


async def test_discovery_finds_enabled_connection_by_domain(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
        },
    )
    await hold_sso_domains(org_admin.org_id, domain)
    found = (await client.get(f"/api/v1/auth/sso/discover?email=jane@{domain}")).json()
    assert found["sso"] is True
    assert str(org_admin.org_id) in found["login_url"]

    # A substring domain must NOT false-match.
    miss = (await client.get(f"/api/v1/auth/sso/discover?email=jane@not{domain}")).json()
    assert miss["sso"] is False

    # An unknown domain → no SSO.
    none = (await client.get("/api/v1/auth/sso/discover?email=jane@unknown.example.com")).json()
    assert none["sso"] is False


async def test_enforced_flag_round_trips_and_surfaces_in_discovery(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = f"acme-{uuid.uuid4().hex[:8]}.example.com"
    await login(client, org_admin.admin_email, org_admin.admin_password)

    # Enable, sign the admin in through the IdP, then ENFORCE.
    body = {
        "oidc_issuer": "https://idp.acme.example.com",
        "oidc_client_id": "cid",
        "oidc_client_secret": "s",
        "enabled": True,
    }
    first = await client.put("/api/v1/org/sso", json={**body, "enforced": False})
    assert first.status_code == 200, first.text
    await hold_sso_domains(org_admin.org_id, domain)
    await signed_in_through_sso(client, org_admin.org_id)
    put = await client.put("/api/v1/org/sso", json={**body, "enforced": True})
    assert put.status_code == 200, put.text
    assert put.json()["enforced"] is True
    assert (await client.get("/api/v1/org/sso")).json()["enforced"] is True
    # Discovery tells the SPA to hide the password form for this domain.
    disc = (await client.get(f"/api/v1/auth/sso/discover?email=jane@{domain}")).json()
    assert disc["sso"] is True
    assert disc["enforced"] is True

    # Relax enforcement (keep enabled; omit the secret to keep the stored one).
    relax = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "enabled": True,
            "enforced": False,
        },
    )
    assert relax.status_code == 200, relax.text
    assert (await client.get("/api/v1/org/sso")).json()["enforced"] is False
    disc2 = (await client.get(f"/api/v1/auth/sso/discover?email=jane@{domain}")).json()
    assert disc2["sso"] is True
    assert disc2["enforced"] is False


async def _member_in_domain(
    org_id: UUID, *, domain: str, password: str = "member-pass-123", platform: bool = False
) -> tuple[str, str, UUID]:
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(
            s,
            org_id=org_id,
            email=f"u-{uuid.uuid4().hex[:8]}@{domain}",
            password=password,
            verified=True,
        )
        if platform:
            from alkera_core.models import PlatformRole

            member.platform_role = PlatformRole.ALKERA_ADMIN
        email, uid = member.email, member.id
        await s.commit()
    assert pw is not None
    return email, pw, uid


async def _enforce_for_domain(client: AsyncClient, org_admin: OrgWithAdmin, domain: str) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for enforced in (False, True):
        if enforced:
            await signed_in_through_sso(client, org_admin.org_id)
        r = await client.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "s",
                "enabled": True,
                "enforced": enforced,
            },
        )
        assert r.status_code == 200, r.text
        await hold_sso_domains(org_admin.org_id, domain)


async def test_password_login_blocked_when_sso_enforced(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email, pw, uid = await _member_in_domain(org_admin.org_id, domain=domain)
    await _enforce_for_domain(client, org_admin, domain)

    # The correct password is rejected — SSO is the only way in for this domain.
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sso_required"

    # A WRONG password is still a plain 401 (not 403) — no enumeration of policy.
    bad = await client.post("/api/v1/auth/login", json={"email": email, "password": "nope"})
    assert bad.status_code == 401

    # A break-glass exemption lets that account back in with its password.
    ex = await client.put(f"/api/v1/org/sso/exemptions/{uid}", json={"exempt": True})
    assert ex.status_code == 200, ex.text
    assert ex.json()["sso_exempt"] is True
    ok = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert ok.status_code == 200


async def test_enforced_sso_allows_platform_staff_and_unenforced_password(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    staff_email, staff_pw, _ = await _member_in_domain(
        org_admin.org_id, domain=domain, platform=True
    )
    await _enforce_for_domain(client, org_admin, domain)
    # Platform staff are never locked out (operator break-glass).
    staff = await client.post(
        "/api/v1/auth/login", json={"email": staff_email, "password": staff_pw}
    )
    assert staff.status_code == 200

    # Relax enforcement → ordinary password login works again.
    member_email, member_pw, _ = await _member_in_domain(org_admin.org_id, domain=domain)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.example.com",
            "oidc_client_id": "cid",
            "enabled": True,
            "enforced": False,
        },
    )
    ok = await client.post(
        "/api/v1/auth/login", json={"email": member_email, "password": member_pw}
    )
    assert ok.status_code == 200


async def test_enforced_requires_enabled(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """enforced=true with enabled=false is a contradiction (it would never take
    effect) and must be rejected rather than stored."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": False,
            "enforced": True,
        },
    )
    assert resp.status_code == 400
    assert "enforced" in resp.text.lower()
