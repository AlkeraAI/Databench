"""A social provider is not silently added as a credential to an MFA-protected account.

The first callback for an unknown provider identity auto-links it to whatever
local account holds the same verified email. That is done FOR the account holder:
they never asked for this provider to become a way into their account. When the
account has deliberately enrolled a second factor, doing it anyway hands a full
session to whoever controls the provider identity on ONE factor — exactly the
compromise (a phished Google password, a hijacked Workspace session) that the
account holder enrolled TOTP to survive — and the new link appears without their
approval.

An identity the account ALREADY holds is a different question and keeps working:
that link was accepted once, and refusing it would strand accounts whose only
credential is the provider. These pin the line between the two, plus the two
carve-outs that keep the refusal from locking anyone out.

Driven through the real start → callback route with the in-process MockProvider,
so the assertions are what a browser observes: the redirect target, whether a
session cookie was set, and whether a link row now exists.
"""

from __future__ import annotations

import secrets
import time
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from alkera_core.auth import COOKIE_NAME
from alkera_core.auth import totp as totp_mod
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OAuthIdentity, User
from backend.auth.oauth.mock import MockProvider
from backend.auth.oauth.profile import FederatedProfile
from backend.services.identity import mfa as mfa_service
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, make_member

pytestmark = pytest.mark.asyncio


async def _oauth_login(email: str, *, subject: str) -> tuple[Response, bool]:
    """A full mock start → callback in its OWN cookie jar. Returns the callback
    response and whether it left the caller signed in."""
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


async def _enroll_mfa(user_id: uuid.UUID) -> str:
    """Put a REAL enrolled second factor on the account, the way
    /mfa/enroll → /mfa/confirm does. Returns the secret."""
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        secret, _uri = mfa_service.begin_enrollment(user)
        mfa_service.confirm_enrollment(user, totp_mod._hotp(secret, int(time.time() // 30)))
        await session.commit()
    return secret


async def _identity_exists(subject: str) -> bool:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(OAuthIdentity.id).where(
                OAuthIdentity.provider == "mock", OAuthIdentity.subject == subject
            )
        )
        return row.first() is not None


async def _reload(user_id: uuid.UUID) -> User:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def test_a_new_provider_identity_is_refused_for_an_account_with_a_second_factor(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The attack: the victim's provider identity is compromised, the attacker
    completes consent, and the callback would otherwise mint a full session on an
    account whose owner asked for two factors. It is refused, no session is set,
    and — just as important — no link is created behind the owner's back."""
    email = f"mfa-link-{uuid.uuid4().hex[:8]}@acme-link.example.com"
    member, _password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    await _enroll_mfa(member.id)
    subject = _subject()

    resp, signed_in = await _oauth_login(email, subject=subject)

    assert "oauth_error=mfa_link_required" in resp.headers["location"]
    assert not signed_in
    assert not await _identity_exists(subject)
    # The refusal leaves the account exactly as it was.
    after = await _reload(member.id)
    assert after.mfa_enabled is True
    assert after.password_hash is not None


async def test_the_same_account_without_a_second_factor_still_auto_links(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetry that shows the gate is about the second factor and nothing
    else: identical account, identical callback, no MFA — it links and signs in."""
    email = f"nomfa-link-{uuid.uuid4().hex[:8]}@acme-link.example.com"
    await make_member(real_session, org_id=org_admin.org_id, email=email, verified=True)
    subject = _subject()

    resp, signed_in = await _oauth_login(email, subject=subject)

    assert "oauth_error" not in resp.headers["location"], resp.headers["location"]
    assert signed_in
    assert await _identity_exists(subject)


async def test_an_identity_the_account_already_holds_keeps_signing_in(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Only the CREATION of a new link is gated. A provider the account already
    signs in with was accepted once; turning on MFA afterwards must not silently
    take that button away — for an account with no password it is the only way
    in, and there is no self-service relink flow to recover through."""
    email = f"linked-{uuid.uuid4().hex[:8]}@acme-link.example.com"
    member, _password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    subject = _subject()

    _first, signed_in = await _oauth_login(email, subject=subject)
    assert signed_in
    await _enroll_mfa(member.id)

    second, signed_in_again = await _oauth_login(email, subject=subject)
    assert "oauth_error" not in second.headers["location"], second.headers["location"]
    assert signed_in_again


async def test_an_account_with_no_password_is_not_fenced_out_by_its_own_factor(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The carve-out that stops the gate becoming a lockout. Without a local
    password the provider is the account's only credential, so refusing the link
    would leave the owner with no way in at all rather than protecting them."""
    email = f"nopw-link-{uuid.uuid4().hex[:8]}@acme-link.example.com"
    member, _password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    await _enroll_mfa(member.id)
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == member.id))).scalar_one()
        user.password_hash = None
        await session.commit()

    _resp, signed_in = await _oauth_login(email, subject=_subject())
    assert signed_in


async def test_a_never_verified_account_is_still_claimed_factor_and_all(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other carve-out. A factor on an account whose address was never proven
    may well have been planted by a squatter, so it is not evidence of the
    rightful owner's intent — the address's proven owner claims the account and
    the eviction clears the factor, exactly as before this gate existed."""
    email = f"squat-link-{uuid.uuid4().hex[:8]}@acme-link.example.com"
    member, _password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=False
    )
    await _enroll_mfa(member.id)

    _resp, signed_in = await _oauth_login(email, subject=_subject())
    assert signed_in

    after = await _reload(member.id)
    assert after.email_verified_at is not None
    assert after.mfa_enabled is False
    assert after.mfa_secret_encrypted is None
    assert after.password_hash is None
