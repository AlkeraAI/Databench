"""End-to-end OAuth flow tests via the in-process MockProvider.

The mock provider lets us drive the real start → callback → register → session
path with zero external calls. Each decision-tree branch is covered here.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OAuthIdentity, User
from backend.auth.oauth.mock import MockProvider
from backend.auth.oauth.profile import FederatedProfile
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, app_client, login, make_member

pytestmark = [
    pytest.mark.asyncio,
    # Every case mints its own subject and email and reads back only the identity
    # it just created, so the cases never meet and may land on different workers.
    pytest.mark.spread,
]


async def _start_and_get_state(
    client: AsyncClient,
    *,
    intent: str = "signup",
    return_to: str = "/dashboard",
    invite: str | None = None,
) -> str:
    """Hit /start and return the `state` the provider would echo back."""
    params: dict[str, str] = {"intent": intent, "return_to": return_to}
    if invite is not None:
        params["invite_token"] = invite
    resp = await client.get("/api/v1/auth/oauth/mock/start", params=params, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    return parse_qs(urlparse(location).query)["state"][0]


async def _callback(
    client: AsyncClient,
    *,
    email: str,
    subject: str | None = None,
    email_verified: bool = True,
    first_name: str = "Given",
    last_name: str = "Family",
    intent: str = "signup",
    return_to: str = "/dashboard",
    invite: str | None = None,
):  # type: ignore[no-untyped-def]
    """Run a full start+callback for the mock provider; return the callback response."""
    state = await _start_and_get_state(client, intent=intent, return_to=return_to, invite=invite)
    code = MockProvider.encode_code(
        FederatedProfile(
            provider="mock",
            subject=subject or email,
            email=email,
            email_verified=email_verified,
            first_name=first_name,
            last_name=last_name,
        )
    )
    return await client.get(
        "/api/v1/auth/oauth/mock/callback",
        params={"state": state, "code": code},
        follow_redirects=False,
    )


def _ticket_from_register_redirect(location: str) -> str:
    assert "/signup" in location
    return parse_qs(urlparse(location).query)["oauth_ticket"][0]


def _subject() -> str:
    """A unique provider subject — the dev DB persists rows across runs, and
    (provider, subject) is unique, so fixed subjects would collide on re-run."""
    return f"sub-{secrets.token_hex(8)}"


# --- discovery -------------------------------------------------------------


async def test_providers_lists_mock(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/oauth/providers")
    assert resp.status_code == 200
    assert "mock" in resp.json()["providers"]


# --- new user → registration ticket → new org -----------------------------


async def test_new_user_redirects_to_register_then_creates_new_org(
    client: AsyncClient, monkeypatch_welcome_send
) -> None:
    email = _unique_email("oauth-new")
    cb = await _callback(client, email=email, first_name="Ada", last_name="Lovelace")
    assert cb.status_code == 302
    ticket = _ticket_from_register_redirect(cb.headers["location"])

    # Prefill context reflects the verified identity.
    ctx = await client.get("/api/v1/auth/oauth/register/context", params={"ticket": ticket})
    assert ctx.status_code == 200
    body = ctx.json()
    assert body["email"] == email
    assert body["first_name"] == "Ada"
    assert body["has_invite"] is False

    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={
            "oauth_ticket": ticket,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "org_name": "Analytical Engines",
        },
    )
    assert reg.status_code == 201, reg.text
    user = reg.json()["user"]
    assert user["email"] == email
    assert user["display_name"] == "Ada Lovelace"
    assert user["has_password"] is False
    # Verified provider email → already verified, no email step.
    assert user["email_verified_at"] is not None
    assert "alkera_session" in client.cookies


async def test_oauth_register_links_identity_and_is_verified(
    client: AsyncClient, monkeypatch_welcome_send
) -> None:
    email = _unique_email("oauth-link")
    subject = _subject()
    cb = await _callback(client, email=email, subject=subject)
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "A", "last_name": "B", "org_name": "Org X"},
    )
    assert reg.status_code == 201
    user_id = reg.json()["user"]["id"]

    async with AsyncSessionLocal() as session:
        identity = (
            await session.execute(
                select(OAuthIdentity).where(
                    OAuthIdentity.provider == "mock", OAuthIdentity.subject == subject
                )
            )
        ).scalar_one()
        assert str(identity.user_id) == user_id
        assert identity.email_verified is True


# --- existing linked identity → login --------------------------------------


async def test_returning_oauth_user_logs_in(client: AsyncClient, monkeypatch_welcome_send) -> None:
    email = _unique_email("oauth-return")
    subject = _subject()
    cb = await _callback(client, email=email, subject=subject)
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "Re", "last_name": "Turn", "org_name": "Org R"},
    )
    client.cookies.clear()

    cb2 = await _callback(client, email=email, subject=subject, intent="login")
    assert cb2.status_code == 302
    # Logged straight in to the return target — no register redirect.
    assert "/signup" not in cb2.headers["location"]
    assert cb2.headers["location"].endswith("/dashboard")
    assert "alkera_session" in client.cookies


# --- existing email, no link → auto-link (verified only) -------------------


async def test_verified_email_autolinks_existing_password_account(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    # A LEGITIMATE, already email-verified password account. Auto-link must
    # attach the identity WITHOUT disturbing the existing credentials — the
    # pre-hijack cleanup is reserved for unverified accounts only.
    member, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    subject = _subject()
    cb = await _callback(
        client, email=member.email, subject=subject, email_verified=True, intent="login"
    )
    assert cb.status_code == 302
    assert "/signup" not in cb.headers["location"]
    assert "alkera_session" in client.cookies

    async with AsyncSessionLocal() as session:
        identity = (
            await session.execute(select(OAuthIdentity).where(OAuthIdentity.subject == subject))
        ).scalar_one()
        assert identity.user_id == member.id

    # The existing password is untouched — the account was already proven, so
    # there is nothing to evict. (Regression guard: don't wipe verified creds.)
    relogin = await client.post("/api/v1/auth/login", json={"email": member.email, "password": pw})
    assert relogin.status_code == 200, relogin.text


async def test_second_provider_account_with_same_email_redirects_already_linked(
    client: AsyncClient, monkeypatch_welcome_send
) -> None:
    """A DIFFERENT provider account (new subject) asserting an email whose user
    already has this provider linked must land on the error redirect, never a 500.
    The unique-constraint violation poisons the transaction, and this callback
    outcome is a clean exit -- the regression was `get_db`'s commit-on-exit
    blowing up (PendingRollbackError) over the poisoned session."""
    email = _unique_email("oauth-second-acct")
    subject = _subject()
    cb = await _callback(client, email=email, subject=subject)
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "A", "last_name": "B", "org_name": "Org 2nd"},
    )
    assert reg.status_code == 201, reg.text
    client.cookies.clear()

    cb2 = await _callback(client, email=email, subject=_subject(), intent="login")
    assert cb2.status_code == 302
    assert "oauth_error=already_linked" in cb2.headers["location"]
    assert "alkera_session" not in client.cookies


async def test_unverified_email_does_not_autolink(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    subject = _subject()
    cb = await _callback(
        client, email=member.email, subject=subject, email_verified=False, intent="login"
    )
    assert cb.status_code == 302
    assert "oauth_error=email_unverified" in cb.headers["location"]
    assert "alkera_session" not in client.cookies
    # The block must not leave a partial identity link behind.
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(OAuthIdentity).where(OAuthIdentity.subject == subject))
        ).scalar_one_or_none()
        assert row is None


# --- account pre-hijack (Classic-Federated Merge) defense -----------------


async def test_password_signup_squat_is_neutralized_by_verified_oauth(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    """Account pre-hijack (Classic-Federated Merge) — the headline regression.

    An attacker squats the victim's address via a bare password signup, which
    yields a usable, *unverified* account without ever proving the email. Later
    the real victim signs in with a verified provider. The verified login must
    CLAIM the account and EVICT the squatter rather than silently merging the
    victim into an attacker-controlled account:
      - the squatter's password is wiped (their password login stops working),
      - every prior squatter session is revoked,
      - the email is stamped verified,
      - the provider identity attaches to the *same* account row,
      - the victim's freshly-minted session still works.

    `client` is the attacker (its jar holds the squatter session); the victim
    drives a separate client so each cookie jar is populated the real way, via
    Set-Cookie. The attacker jar is never cleared, so it still carries the
    soon-to-be-revoked session for the final check.
    """
    victim_email = _unique_email("prehijack")
    attacker_password = "attacker-knows-this-12345"

    # 1. Attacker squats the address (unverified account, attacker-chosen pw).
    squat = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": victim_email,
            "first_name": "Squatter",
            "last_name": "Account",
            "password": attacker_password,
            "org_name": "Squatted Org",
        },
    )
    assert squat.status_code == 201, squat.text
    squatted_user_id = squat.json()["user"]["id"]
    assert squat.json()["user"]["email_verified_at"] is None  # never proven
    # The squatter's session is live right now.
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # 2. The real victim signs in with a verified provider (own cookie jar).
    subject = _subject()
    async with app_client() as victim:
        cb = await _callback(
            victim, email=victim_email, subject=subject, email_verified=True, intent="login"
        )
        assert cb.status_code == 302
        assert "/signup" not in cb.headers["location"]  # logged in, not re-register
        assert COOKIE_NAME in victim.cookies
        # Victim's brand-new session works (survives the claim's revoke-all).
        assert (await victim.get("/api/v1/auth/me")).status_code == 200

    # 3a. The provider identity attached to the SAME (squatted) account row.
    async with AsyncSessionLocal() as session:
        identity = (
            await session.execute(select(OAuthIdentity).where(OAuthIdentity.subject == subject))
        ).scalar_one()
        assert str(identity.user_id) == squatted_user_id
        user = (await session.execute(select(User).where(User.id == identity.user_id))).scalar_one()
        # 3b. Email is now verified and the squatter's password is GONE.
        assert user.email_verified_at is not None
        assert user.password_hash is None

    # 3c. The attacker can no longer authenticate with the password they set.
    relogin = await client.post(
        "/api/v1/auth/login",
        json={"email": victim_email, "password": attacker_password},
    )
    assert relogin.status_code == 401

    # 3d. The attacker's pre-existing session (still in this jar) was revoked.
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_register_rejects_a_session_token_as_ticket(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    # A valid session JWT (wrong `typ`) must NOT be accepted as a register
    # ticket — guards the token-type-confusion boundary.
    from uuid import uuid4

    from alkera_core.auth import encode_session_token

    token, _claims = encode_session_token(
        user_id=uuid4(), email="x@y.com", org_team_id=uuid4(), platform_role=None
    )
    resp = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": token, "first_name": "A", "last_name": "B", "org_name": "O"},
    )
    assert resp.status_code == 400


async def test_an_unverified_provider_email_is_never_offered_a_signup(client: AsyncClient) -> None:
    # A provider that has not verified the address proves nothing about who
    # owns it, so no registration ticket is minted for it: the person is sent
    # back to sign-in with the reason, and nothing is created.
    email = _unique_email("unverified-new")
    cb = await _callback(client, email=email, subject=_subject(), email_verified=False)
    assert cb.status_code == 302
    assert "oauth_error=email_unverified" in cb.headers["location"]
    assert "oauth_ticket" not in cb.headers["location"]
    assert COOKIE_NAME not in client.cookies
    async with AsyncSessionLocal() as session:
        assert (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none() is None


async def test_register_refuses_a_ticket_for_an_unverified_email(client: AsyncClient) -> None:
    # A ticket minted before the refusal above (they live for minutes) is
    # refused when redeemed: no account, no link, no session.
    from alkera_core.auth.tokens import encode_register_ticket

    email = _unique_email("unverified-ticket")
    subject = _subject()
    ticket = encode_register_ticket(
        provider="mock",
        subject=subject,
        email=email,
        email_verified=False,
        first_name="U",
        last_name="V",
        invite_token=None,
    )
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "U", "last_name": "V", "org_name": "Org UV"},
    )
    assert reg.status_code == 403, reg.text
    assert reg.json()["error"]["code"] == "email_unverified"
    assert COOKIE_NAME not in client.cookies
    async with AsyncSessionLocal() as session:
        assert (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none() is None
        assert (
            await session.execute(select(OAuthIdentity).where(OAuthIdentity.subject == subject))
        ).scalar_one_or_none() is None


async def test_a_verified_claim_drops_every_link_the_squatter_left(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    """A squatter's provider link does not outlive the real owner's claim.

    The squatter holds an unverified account at the victim's address with a
    provider identity linked to it (a GitHub account that lists the address
    without verifying it; rows like that exist from before registration
    refused them). The victim signs in with a provider that verified the
    address, which claims the account. The squatter's provider sign-in must
    then fail: the claim removes every identity but the one that proved the
    address."""
    from backend.services.identity import oauth as oauth_service

    victim_email = _unique_email("squat-link")
    squat = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": victim_email,
            "first_name": "Squatter",
            "last_name": "Account",
            "password": "attacker-knows-this-12345",
            "org_name": "Squatted Org",
        },
    )
    assert squat.status_code == 201, squat.text
    squatted_id = squat.json()["user"]["id"]
    squatter_subject = _subject()
    async with AsyncSessionLocal() as session:
        session.add(
            OAuthIdentity(
                user_id=squatted_id,
                provider="mock",
                subject=squatter_subject,
                email_at_link=victim_email,
                email_verified=False,
            )
        )
        await session.commit()

    # The victim proves the address through another provider.
    victim_subject = _subject()
    async with AsyncSessionLocal() as session:
        outcome = await oauth_service.resolve(
            session,
            FederatedProfile(
                provider="google",
                subject=victim_subject,
                email=victim_email,
                email_verified=True,
            ),
            invite_token=None,
        )
        await session.commit()
    assert isinstance(outcome, oauth_service.LoginOutcome)
    assert str(outcome.user.id) == squatted_id

    async with app_client() as squatter:
        cb = await _callback(
            squatter,
            email=victim_email,
            subject=squatter_subject,
            email_verified=False,
            intent="login",
        )
        assert cb.status_code == 302
        assert "oauth_error=" in cb.headers["location"]
        assert COOKIE_NAME not in squatter.cookies

    async with AsyncSessionLocal() as session:
        links = (
            (
                await session.execute(
                    select(OAuthIdentity.provider).where(OAuthIdentity.user_id == outcome.user.id)
                )
            )
            .scalars()
            .all()
        )
    assert links == ["google"]


# --- CSRF / replay / open-redirect -----------------------------------------


async def test_callback_with_wrong_state_is_rejected(client: AsyncClient) -> None:
    await _start_and_get_state(client)  # sets the tx cookie
    code = MockProvider.encode_code(
        FederatedProfile(provider="mock", subject="x", email="x@y.com", email_verified=True)
    )
    resp = await client.get(
        "/api/v1/auth/oauth/mock/callback",
        params={"state": "not-the-real-state", "code": code},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "oauth_error=state" in resp.headers["location"]


async def test_callback_without_tx_cookie_is_expired(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/auth/oauth/mock/callback",
        params={"state": "whatever", "code": "whatever"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "oauth_error=expired" in resp.headers["location"]


async def test_return_to_open_redirect_is_sanitized(client: AsyncClient) -> None:
    email = _unique_email("oauth-redir")
    subject = _subject()
    # Register first so the second callback is a login (which honors return_to).
    cb = await _callback(client, email=email, subject=subject)
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "R", "last_name": "D", "org_name": "Org RD"},
    )
    client.cookies.clear()

    cb2 = await _callback(
        client, email=email, subject=subject, intent="login", return_to="https://evil.example.com/x"
    )
    assert cb2.status_code == 302
    location = cb2.headers["location"]
    assert "evil.example.com" not in location
    assert location.endswith("/dashboard")


async def test_register_ticket_replay_is_rejected(client: AsyncClient) -> None:
    email = _unique_email("oauth-replay")
    cb = await _callback(client, email=email, subject=_subject())
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    body = {"oauth_ticket": ticket, "first_name": "R", "last_name": "P", "org_name": "Org RP"}
    first = await client.post("/api/v1/auth/oauth/register", json=body)
    assert first.status_code == 201
    second = await client.post("/api/v1/auth/oauth/register", json=body)
    assert second.status_code == 409


async def test_register_with_garbage_ticket_is_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": "not-a-jwt", "first_name": "A", "last_name": "B", "org_name": "O"},
    )
    assert resp.status_code == 400


# --- registration via invite -----------------------------------------------


async def test_oauth_register_via_invite_joins_org(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch_email_send: list[dict],
) -> None:
    # Org admin invites a fresh email.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    invitee = _unique_email("oauth-invitee")
    inv = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/invitations",
        json={"email": invitee, "role": "member"},
    )
    assert inv.status_code == 201, inv.text
    invite_token = monkeypatch_email_send[0]["invitation_token"]
    client.cookies.clear()

    # The invitee signs up with the provider (email matches the invite).
    cb = await _callback(client, email=invitee, subject=_subject(), invite=invite_token)
    ticket = _ticket_from_register_redirect(cb.headers["location"])
    ctx = await client.get("/api/v1/auth/oauth/register/context", params={"ticket": ticket})
    assert ctx.json()["has_invite"] is True

    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "In", "last_name": "Vitee"},
    )
    assert reg.status_code == 201, reg.text
    # Joined the inviting org, not a new one.
    assert reg.json()["user"]["org_team_id"] == str(org_admin.org_id)


# --- mock authorize page (dev click-through) -------------------------------


async def test_mock_authorize_page_renders_and_bounces(client: AsyncClient) -> None:
    state = await _start_and_get_state(client)
    # Render the form.
    # Derive from settings.oauth_redirect_base (not a hard-coded :8000) so the
    # route's redirect_uri allowlist check passes regardless of the API port —
    # e.g. an isolated per-worktree port from .env.workspace.
    redirect_uri = f"{settings.oauth_redirect_base}/api/v1/auth/oauth/mock/callback"
    page = await client.get(
        "/api/v1/auth/oauth/mock/authorize",
        params={"state": state, "redirect_uri": redirect_uri},
    )
    assert page.status_code == 200
    assert "Mock provider sign-in" in page.text
    # Submit it → bounce to the callback with an encoded code.
    submitted = await client.get(
        "/api/v1/auth/oauth/mock/authorize",
        params={
            "state": state,
            "redirect_uri": redirect_uri,
            "email": _unique_email("mockform"),
            "first_name": "Form",
            "last_name": "User",
            "submit": "1",
        },
        follow_redirects=False,
    )
    assert submitted.status_code == 302
    assert submitted.headers["location"].startswith(redirect_uri)
    assert "code=" in submitted.headers["location"]


async def test_unknown_provider_start_redirects_with_error(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/auth/oauth/nope/start", params={"intent": "login"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert "oauth_error=unknown_provider" in resp.headers["location"]


# --- business-email gate ---------------------------------------------------


def _ticket_from_location(location: str) -> str:
    return parse_qs(urlparse(location).query)["oauth_ticket"][0]


async def test_new_personal_email_user_redirects_to_business_email_page(
    client: AsyncClient,
) -> None:
    email = f"oauth-personal-{secrets.token_hex(6)}@gmail.com"
    cb = await _callback(client, email=email, subject=_subject())
    assert cb.status_code == 302
    location = cb.headers["location"]
    # Personal email → the dedicated "use your work email" page, NOT /signup.
    assert "/oauth/business-email" in location
    assert "oauth_ticket=" in location
    assert "alkera_session" not in client.cookies


async def test_returning_personal_email_user_is_not_gated(client: AsyncClient) -> None:
    email = f"oauth-personal-ret-{secrets.token_hex(6)}@gmail.com"
    subject = _subject()
    cb = await _callback(client, email=email, subject=subject)
    ticket = _ticket_from_location(cb.headers["location"])
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={
            "oauth_ticket": ticket,
            "first_name": "P",
            "last_name": "E",
            "org_name": "Personal Org",
            "allow_personal_email": True,
        },
    )
    assert reg.status_code == 201, reg.text
    client.cookies.clear()

    # A returning login with the same personal email is NOT gated.
    cb2 = await _callback(client, email=email, subject=subject, intent="login")
    assert cb2.status_code == 302
    assert "/oauth/business-email" not in cb2.headers["location"]
    assert "/signup" not in cb2.headers["location"]
    assert "alkera_session" in client.cookies


async def test_oauth_register_blocks_personal_email_without_flag(client: AsyncClient) -> None:
    email = f"oauth-reg-block-{secrets.token_hex(6)}@gmail.com"
    cb = await _callback(client, email=email, subject=_subject())
    ticket = _ticket_from_location(cb.headers["location"])
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "P", "last_name": "E", "org_name": "Org PE"},
    )
    assert reg.status_code == 403
    assert reg.json()["error"]["code"] == "personal_email"


async def test_oauth_register_allows_personal_email_with_flag(client: AsyncClient) -> None:
    email = f"oauth-reg-ok-{secrets.token_hex(6)}@gmail.com"
    cb = await _callback(client, email=email, subject=_subject())
    ticket = _ticket_from_location(cb.headers["location"])
    reg = await client.post(
        "/api/v1/auth/oauth/register",
        json={
            "oauth_ticket": ticket,
            "first_name": "P",
            "last_name": "E",
            "org_name": "Org OK",
            "allow_personal_email": True,
        },
    )
    assert reg.status_code == 201, reg.text
    assert reg.json()["user"]["email"] == email
