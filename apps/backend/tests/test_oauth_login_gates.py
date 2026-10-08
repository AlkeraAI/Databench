"""What a federated sign-in is allowed to bypass — and what it deliberately is not.

`POST /auth/login` refuses a password when the org enforces SSO. That gate used to
live ONLY on that branch, so a linked Google/GitHub identity walked straight past it
into a 30-day session (and, via the device-code flow, a 90-day CLI bearer). These pin
that the shared decision layer — `oauth_service.resolve`, which every non-SSO callback
funnels through — applies it, and pin the carve-outs that keep it from locking anyone
out.

They also pin the deliberate NON-gate: on an identity the account ALREADY holds, its
app-level TOTP is not demanded on this leg, because app MFA is the second factor on
the local password credential and a provider sign-in is a separate chain the IdP
already authenticated. (Minting a BRAND-NEW link into an MFA-protected account is a
different question and is refused — see test_oauth_mfa_link_gate.) The control for an
org that wants a hard guarantee over every sign-in is SSO enforcement, which these
show does cover the social button.

Driven through the real start → callback route with the in-process MockProvider, so
the assertions are what a browser observes: the redirect target and whether a session
cookie was set.
"""

from __future__ import annotations

import secrets
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.auth import totp as totp_mod
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OAuthIdentity, User
from backend.auth.oauth.mock import MockProvider
from backend.auth.oauth.profile import FederatedProfile
from backend.services.identity import mfa as mfa_service
from backend.services.identity import sso as sso_service
from httpx import AsyncClient, Response
from sqlalchemy import select
from tests.conftest import (
    OrgWithAdmin,
    TotpClock,
    app_client,
    hold_sso_domains,
    login,
    make_member,
    signed_in_through_sso,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _sso_feature_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO config is an Enterprise surface; these tests configure it, so run them
    on a self-hosted deployment (the SaaS gate itself is test_enterprise_gating)."""
    monkeypatch.setattr(settings, "self_hosted", True)
    # Self-hosted is invite-only by default; these tests sign strangers up.
    monkeypatch.setattr(settings, "signup_mode", "open")


async def _oauth_login(email: str, *, subject: str) -> tuple[Response, bool]:
    """A full mock start → callback in its OWN cookie jar (so the admin's jar is
    never reused). Returns the callback response and whether it left the caller
    logged in — the two things a browser observes."""
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
                subject=subject,
                email=email,
                email_verified=True,
                first_name="Given",
                last_name="Family",
            )
        )
        resp = await c.get(
            "/api/v1/auth/oauth/mock/callback",
            params={"state": state, "code": code},
            follow_redirects=False,
        )
        return resp, COOKIE_NAME in c.cookies


def _subject() -> str:
    return f"sub-{secrets.token_hex(8)}"


async def _user(email: str) -> User:
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one()


async def _set_user(email: str, **fields: object) -> None:
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        for key, value in fields.items():
            setattr(user, key, value)
        await s.commit()


async def _another_org() -> OrgWithAdmin:
    """A SECOND org whose admin can drive the SSO config route (which demands a
    verified email) — the foreign tenant in the cross-tenant cases."""
    from datetime import UTC, datetime

    from backend.services.org import teams as team_service

    email = f"other-admin-{uuid.uuid4().hex[:8]}@alkera.dev"
    password = "admin-pass-12345"
    async with AsyncSessionLocal() as s:
        org, admin = await team_service.create_org_with_admin(
            s,
            org_name=f"other-org-{uuid.uuid4().hex[:8]}",
            admin_email=email,
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password=password,
        )
        admin.email_verified_at = datetime.now(UTC)
        await s.commit()
        return OrgWithAdmin(
            org_id=org.id, admin_id=admin.id, admin_email=email, admin_password=password
        )


async def _enforce_sso(client: AsyncClient, org: OrgWithAdmin, domain: str) -> None:
    """Configure the org's IdP, sign the admin in through it, then enforce it:
    the route refuses enforcement from an admin who has not."""
    await login(client, org.admin_email, org.admin_password)
    for enforced in (False, True):
        if enforced:
            await signed_in_through_sso(client, org.org_id)
        r = await client.put(
            "/api/v1/org/sso",
            json={
                "oidc_issuer": "https://idp.acme.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "s",
                "enabled": True,
                "enforced": enforced,
            },
        )
        assert r.status_code == 200, r.text
        await hold_sso_domains(org.org_id, domain)


# --------------------------------------------------------------------------- #
# SSO enforcement
# --------------------------------------------------------------------------- #


async def test_enforced_sso_also_blocks_the_social_oauth_callback(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """`enforced` is what an org buys to route every sign-in through its IdP (and
    with it MFA policy, conditional access, offboarding). A linked Google account
    must not be a second front door."""
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp-{uuid.uuid4().hex[:8]}@{domain}"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    await _enforce_sso(client, org_admin, domain)

    resp, signed_in = await _oauth_login(email, subject=_subject())
    assert resp.status_code == 302
    assert "oauth_error=sso_required" in resp.headers["location"]
    assert not signed_in


async def test_another_orgs_domain_claim_cannot_block_a_social_login(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The tenant boundary, in the direction that costs availability: a domain
    claim is free text with no ownership proof, so enforcement is resolved from the
    ACCOUNT'S OWN org — never from whichever connection on the deployment happens
    to claim the email's domain. Otherwise any tenant could type a competitor's
    domain into `allowed_domains` and refuse that domain's users everywhere. The
    positive direction (their OWN org's enforced connection does block them) is the
    test above."""
    domain = f"victim-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp-{uuid.uuid4().hex[:8]}@{domain}"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)

    squatter = await _another_org()
    await _enforce_sso(client, squatter, domain)

    resp, signed_in = await _oauth_login(email, subject=_subject())
    assert "oauth_error" not in resp.headers["location"], resp.headers["location"]
    assert signed_in


async def test_an_idp_provisioned_account_stays_governed_after_an_email_rename(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The other binding: an account that already holds an identity federated by
    ITS OWN org's IdP is governed even once its address sits outside
    `allowed_domains` — otherwise a self-service rename would quietly hand an
    IdP-provisioned account a second front door (the same rule the password branch
    applies)."""
    claimed = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email = f"renamed-{uuid.uuid4().hex[:8]}@elsewhere-{uuid.uuid4().hex[:6]}.example.com"
    async with AsyncSessionLocal() as s:
        user, _pw = await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
        s.add(
            OAuthIdentity(
                user_id=user.id,
                provider=sso_service.provider_key(org_admin.org_id),
                subject=uuid.uuid4().hex,
                email_at_link=email,
                email_verified=True,
            )
        )
        await s.commit()
    await _enforce_sso(client, org_admin, claimed)

    resp, signed_in = await _oauth_login(email, subject=_subject())
    assert "oauth_error=sso_required" in resp.headers["location"]
    assert not signed_in


async def test_a_break_glass_exemption_still_admits_the_social_callback(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: the same carve-outs password login has — an explicit
    exemption — so a broken IdP can never lock an org out."""
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp-{uuid.uuid4().hex[:8]}@{domain}"
    async with AsyncSessionLocal() as s:
        user, _pw = await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
        user_id = user.id
    await _enforce_sso(client, org_admin, domain)
    ex = await client.put(f"/api/v1/org/sso/exemptions/{user_id}", json={"exempt": True})
    assert ex.status_code == 200, ex.text

    resp, signed_in = await _oauth_login(email, subject=_subject())
    assert "oauth_error" not in resp.headers["location"]
    assert signed_in


async def test_an_unenforced_connection_does_not_block_anything(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp-{uuid.uuid4().hex[:8]}@{domain}"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    r = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
            "enforced": False,
        },
    )
    assert r.status_code == 200, r.text
    await hold_sso_domains(org_admin.org_id, domain)
    _resp, signed_in = await _oauth_login(email, subject=_subject())
    assert signed_in


# --------------------------------------------------------------------------- #
# The account's second factor — deliberately NOT demanded on this leg
# --------------------------------------------------------------------------- #
#
# App-level TOTP is the second factor on the LOCAL PASSWORD credential: `auth.login`
# asks for it because a password alone is weak. A federated sign-in the account has
# already accepted is a different credential chain the provider has authenticated,
# with whatever factors the user keeps there — the same split every product that
# offers a "Sign in with Google" button makes, and the reason `resolve` does not
# prompt for a code on an identity the account already holds.
#
# CREATING a brand-new link into an MFA-protected account is NOT covered by that
# reasoning: nobody asked for the provider to become a credential, so it is refused
# (`oauth_error=mfa_link_required`). The already-held identity keeps working.
#
# This is a decision, not an omission. The control for a tenant that wants a hard
# guarantee over EVERY sign-in is SSO enforcement (above): it is server-side, it is
# per-org, and — unlike a post-redirect code prompt — it also covers the accounts
# whose only credential IS the provider. "Require app MFA even on a social login" is
# a separate, opt-in org setting; it needs a step-up round trip (a signed challenge
# ticket + an endpoint that re-drives `resolve` with the code) that does not exist.


async def test_a_provider_signs_in_an_account_that_has_a_second_factor_enrolled(
    org_admin: OrgWithAdmin,
    totp_clock: TotpClock,
) -> None:
    """The provider button keeps working on the branch that is not gated: an
    identity the account ALREADY holds signs in with no code demanded, even after
    the owner enrolls a second factor. The other branch is pinned here from the
    same account so the line between them is visible — a DIFFERENT provider
    identity would be a new credential the owner never approved, and is refused
    with no session and no link left behind. Enrollment is real — secret + backup
    codes via the same calls /mfa/enroll → /mfa/confirm make — so this cannot pass
    on a half-configured account."""
    email = f"mfa-{uuid.uuid4().hex[:8]}@acme-mfa.example.com"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    subject = _subject()

    # Branch (b): a provider account matching the verified address auto-links,
    # while the account still has a single factor.
    first, signed_in = await _oauth_login(email, subject=subject)
    assert "oauth_error" not in first.headers["location"], first.headers["location"]
    assert signed_in
    assert await _identity_exists(subject)

    await _enroll_mfa(email, totp_clock)

    # Branch (a): the SAME linked identity on the next visit — no code prompt.
    second, signed_in = await _oauth_login(email, subject=subject)
    assert "oauth_error" not in second.headers["location"], second.headers["location"]
    assert signed_in

    # Back to branch (b), now that a second factor exists: a new link is refused.
    unlinked = _subject()
    third, signed_in_new = await _oauth_login(email, subject=unlinked)
    assert "oauth_error=mfa_link_required" in third.headers["location"]
    assert not signed_in_new
    assert not await _identity_exists(unlinked)


async def test_the_password_branch_still_demands_the_code_after_a_provider_login(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    totp_clock: TotpClock,
) -> None:
    """The asymmetry that makes the split defensible: not asking on the federated
    leg must not soften the local credential. The SAME account's password login
    still refuses without a code and admits with one, and the enrolled factor
    survives the provider round trip untouched (nothing consumed, nothing reset).
    The provider is linked before the factor is enrolled, since minting a new link
    into an MFA-protected account is refused."""
    email = f"mfa-local-{uuid.uuid4().hex[:8]}@acme-mfa.example.com"
    async with AsyncSessionLocal() as s:
        _member, password = await make_member(
            s, org_id=org_admin.org_id, email=email, verified=True
        )
    assert password is not None
    subject = _subject()
    _linked, signed_in = await _oauth_login(email, subject=subject)
    assert signed_in

    secret, _backup = await _enroll_mfa(email, totp_clock)

    _resp, signed_in = await _oauth_login(email, subject=subject)
    assert signed_in

    no_code = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert no_code.status_code == 401
    assert no_code.json()["error"]["code"] == "mfa_required"

    with_code = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "mfa_code": totp_clock.next_code(secret)},
    )
    assert with_code.status_code == 200, with_code.text


async def test_sso_enforcement_is_the_control_that_covers_the_social_button(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    totp_clock: TotpClock,
) -> None:
    """The answer given to an org that wants its own factors on every sign-in,
    demonstrated on exactly the account the removed code prompt would have covered:
    with enforcement on, the social provider is refused outright — no code to
    collect, no second front door, and it holds for password-less accounts too."""
    domain = f"enf-{uuid.uuid4().hex[:8]}.example.com"
    email = f"emp-{uuid.uuid4().hex[:8]}@{domain}"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    await _enroll_mfa(email, totp_clock)
    await _enforce_sso(client, org_admin, domain)

    resp, signed_in = await _oauth_login(email, subject=_subject())
    assert "oauth_error=sso_required" in resp.headers["location"]
    assert not signed_in


async def _enroll_mfa(email: str, clock: TotpClock) -> tuple[str, list[str]]:
    """Put a REAL enrolled second factor on the account (secret + backup codes),
    the same way /mfa/enroll → /mfa/confirm does. Returns (secret, backup codes).

    Confirmation SPENDS the code it accepts, so a later step-up in the same flow
    reads the next step off `clock`.
    """
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        secret, _uri = mfa_service.begin_enrollment(user)
        codes = mfa_service.confirm_enrollment(user, clock.code(secret))
        await s.commit()
    return secret, codes


async def _identity_exists(subject: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(
            select(OAuthIdentity.id).where(
                OAuthIdentity.provider == "mock", OAuthIdentity.subject == subject
            )
        )
        return row.first() is not None


async def test_an_account_with_no_password_keeps_its_provider_login(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    totp_clock: TotpClock,
) -> None:
    """The same rule seen from the other side. A provider-created account (OAuth
    register, SSO JIT, invite) has no local password for app MFA to be a second
    factor ON, so there is nothing for this leg to demand — and demanding one would
    strand the owner behind their only credential."""
    email = f"mfa-nopw-{uuid.uuid4().hex[:8]}@acme-mfa.example.com"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    await _enroll_mfa(email, totp_clock)
    await _set_user(email, password_hash=None)

    _resp, signed_in = await _oauth_login(email, subject=_subject())
    assert signed_in


# --------------------------------------------------------------------------- #
# The password lockout does not reach the federated leg
# --------------------------------------------------------------------------- #


async def test_a_password_lockout_does_not_take_the_provider_button_away(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    totp_clock: TotpClock,
) -> None:
    """The lockout throttles a CREDENTIAL, not the account. Extending it to the
    federated leg would let anyone who knows an address spend three wrong passwords
    to take that user's Google sign-in away for the whole cool-off — a denial the
    password lockout's own tradeoff never signed up for, since the victim's other
    credential is exactly what makes a lockout survivable."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email = f"pwlock-{uuid.uuid4().hex[:8]}@acme-mfa.example.com"
    async with AsyncSessionLocal() as s:
        await make_member(s, org_id=org_admin.org_id, email=email, verified=True)
    subject = _subject()
    _linked, signed_in = await _oauth_login(email, subject=subject)
    assert signed_in
    await _enroll_mfa(email, totp_clock)

    for _ in range(3):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
        assert r.status_code == 401
    locked = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"

    _resp, signed_in = await _oauth_login(email, subject=subject)
    assert signed_in


# --------------------------------------------------------------------------- #
# Pre-hijack eviction covers the second factor too
# --------------------------------------------------------------------------- #


async def test_a_squatters_mfa_enrollment_is_evicted_with_their_password(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    """The squat is only neutralized if EVERY credential the squatter planted goes.
    A left-behind TOTP secret is an authentication factor the attacker keeps
    forever (backup codes never expire) and the victim cannot remove: /mfa/disable
    demands a code only the attacker has, and no admin reset exists."""
    email = f"squat-{uuid.uuid4().hex[:8]}@acme-squat.example.com"
    squat = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Squatter",
            "last_name": "Account",
            "password": "attacker-knows-this-12345",
            "org_name": f"Squatted {uuid.uuid4().hex[:6]}",
        },
    )
    assert squat.status_code == 201, squat.text

    # The premise: an unverified account CAN plant a second factor today.
    enroll = await client.post("/api/v1/auth/mfa/enroll")
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    code = totp_mod._hotp(secret, int(time.time() // 30))
    confirm = await client.post("/api/v1/auth/mfa/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["backup_codes"]) == 10
    assert (await _user(email)).mfa_enabled is True

    # The rightful owner arrives with a provider-verified email and claims it.
    _claimed, signed_in = await _oauth_login(email, subject=_subject())
    assert signed_in  # the MFA gate must not fence the owner out here

    user = await _user(email)
    assert user.email_verified_at is not None
    assert user.password_hash is None
    assert user.mfa_enabled is False
    assert user.mfa_secret_encrypted is None
    assert user.mfa_backup_codes is None
    # The attacker's backup codes are worthless: nothing to verify against.
    status_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "attacker-knows-this-12345"},
    )
    assert status_resp.status_code == 401
