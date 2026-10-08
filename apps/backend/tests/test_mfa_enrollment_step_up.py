"""Planting a second factor demands proof of a current one.

Turning MFA on is the most permanent thing a session can do to an account, and it
used to be the only credential-grade operation that asked for nothing. Everything
that takes a factor away demands one — `/auth/mfa/disable` needs a current code,
`PATCH /users/{id}` needs the password and the code — while enabling it needed
only a cookie. That asymmetry is what makes a briefly-borrowed session permanent:
a factor enrolled by someone else cannot be removed by the account owner (they
cannot produce its code, a password reset does not clear it, and there is no
admin reset), so the owner is locked out of a verified account for good.

These drive the enroll route with the credential an attacker in that story
actually holds — a session token minted long ago, the shape of a copied CLI
bearer out of `~/.alkera/auth.yml` or a browser left signed in on a shared
machine — and pin that it is refused, while the flows a real user goes through
are untouched.
"""

from __future__ import annotations

import time
import uuid

import pytest
from alkera_core.auth import encode_cli_token, encode_session_token, register_token, totp
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import TokenType, User
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

# Comfortably past the enroll route's freshness allowance.
_STALE_AGE_SECONDS = 6 * 60 * 60


async def _stale_session_token(user: User) -> str:
    """A registered, unexpired bearer credential minted hours ago — the shape of
    the CLI token in `~/.alkera/auth.yml`, whose holder authenticated at some
    point in the past and may no longer be the account owner."""
    token, claims = encode_cli_token(
        user_id=user.id,
        email=user.email,
        org_team_id=user.home_org_team_id,
        platform_role=user.platform_role,
        now=int(time.time()) - _STALE_AGE_SECONDS,
    )
    async with AsyncSessionLocal() as session:
        await register_token(session, claims=claims, token_type=TokenType.CLI)
        await session.commit()
    return token


async def _enroll(token: str, **body: object) -> tuple[int, dict]:
    """POST /auth/mfa/enroll with a Bearer credential, in its own cookie jar so
    no other test's session can answer for it."""
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/auth/mfa/enroll",
            json=body or None,
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.status_code, resp.json()


async def _reload(user_id: uuid.UUID) -> User:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def test_a_fresh_sign_in_still_enrolls_with_nothing_extra(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The flow a real user walks — sign in, open security settings, turn 2FA on —
    asks for nothing beyond the sign-in they just completed."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, member.email, password)

    enroll = await client.post("/api/v1/auth/mfa/enroll")
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    confirm = await client.post(
        "/api/v1/auth/mfa/confirm",
        json={"code": totp._hotp(secret, int(time.time() // 30))},
    )
    assert confirm.status_code == 200, confirm.text
    assert (await _reload(member.id)).mfa_enabled is True


async def test_an_old_session_alone_cannot_plant_a_second_factor(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The attack: a session lifted long after the owner authenticated. It is
    refused before a secret is ever minted, so there is nothing pending for the
    holder to confirm afterwards."""
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    stale = await _stale_session_token(member)

    code, body = await _enroll(stale)
    assert code == 403, body
    assert body["error"]["code"] == "current_password_required"

    after = await _reload(member.id)
    assert after.mfa_secret_encrypted is None
    assert after.mfa_enabled is False


async def test_the_owners_password_unlocks_enrollment_from_an_old_session(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Proof of the account's real credential is the other accepted proof, so a
    long-running session does not force a sign-out to enable 2FA."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    stale = await _stale_session_token(member)

    code, body = await _enroll(stale, current_password=password)
    assert code == 200, body
    assert body["secret"]


async def test_a_wrong_password_is_a_guess_and_spends_the_lockout_budget(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step-up verifies the real password, so leaving it unthrottled would
    hand the very attacker it defends against unlimited guesses at it. Wrong
    attempts go through the same budget `/auth/login` uses; past the threshold the
    route refuses before hashing anything."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    stale = await _stale_session_token(member)

    for _ in range(3):
        code, body = await _enroll(stale, current_password="not-the-password-12345")
        assert code == 403, body
        assert body["error"]["code"] == "current_password_invalid"

    locked_code, locked_body = await _enroll(stale, current_password="not-the-password-12345")
    assert locked_code == 429, locked_body
    assert locked_body["error"]["code"] == "account_locked"
    assert (await _reload(member.id)).mfa_secret_encrypted is None


async def test_an_absent_password_is_a_prompt_not_a_guess(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that simply hasn't collected the password yet must not be able to
    lock the owner out of their own account by retrying the prompt."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    stale = await _stale_session_token(member)

    for _ in range(5):
        code, body = await _enroll(stale)
        assert code == 403, body
        assert body["error"]["code"] == "current_password_required"

    after = await _reload(member.id)
    assert after.failed_login_count == 0
    assert after.locked_until is None


async def test_a_provider_only_account_is_asked_to_sign_in_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An account whose only credential is a federated provider has no password to
    present, so the answer is a re-authentication rather than a dead end."""
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    member.password_hash = None
    await real_session.commit()
    stale = await _stale_session_token(member)

    code, body = await _enroll(stale)
    assert code == 403, body
    assert body["error"]["code"] == "reauth_required"

    fresh, fresh_claims = encode_session_token(
        user_id=member.id,
        email=member.email,
        org_team_id=member.home_org_team_id,
        platform_role=member.platform_role,
    )
    async with AsyncSessionLocal() as session:
        await register_token(session, claims=fresh_claims, token_type=TokenType.SESSION)
        await session.commit()
    # The carve-out cuts both ways: with a just-minted credential the same
    # password-less account enrolls normally.
    ok_code, ok_body = await _enroll(fresh)
    assert ok_code == 200, ok_body
