"""Per-account cooldown on the self-service outbound-email routes.

Verification resends, the authenticated password-link route and the public
password-reset request all send mail on demand with no credential cost. Nothing
else in the stack bounds them (there is no application rate limiter, and the
prod SES sending quota is account-wide, so one abusive tenant's flood starves
every other tenant's invitations and resets). These pin the cooldown, that it is
per-account rather than global, and that the public route keeps its constant,
non-enumerating response.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

#: One second past the verification-resend window (5 min by default — longer
#: than the 60s password-link cooldown because verification resends are the one
#: outbound-email surface a throwaway-signup loop can drive).
_PAST_VERIFICATION_COOLDOWN = settings.email_verification_resend_cooldown_seconds + 1


async def _signup(client: AsyncClient, email: str) -> None:
    suffix = secrets.token_hex(4)
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Throttle",
            "last_name": "Tester",
            "password": "vaultkey-12345",
            "org_name": f"ThrottleCo {suffix}",
        },
    )
    assert resp.status_code == 201, resp.text


async def _age_pending_tokens(email: str, *, seconds: int) -> None:
    """Backdate the pending tokens' issue time by moving their expiry back —
    the same thing the passage of `seconds` real time would do."""
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        shift = timedelta(seconds=seconds)
        if user.email_verification_expires_at is not None:
            user.email_verification_expires_at -= shift
        if user.password_reset_expires_at is not None:
            user.password_reset_expires_at -= shift
        await s.commit()


async def test_verification_resend_is_refused_inside_the_cooldown(
    client: AsyncClient, monkeypatch_verification_send: list[dict], monkeypatch_welcome_send
) -> None:
    email = f"resend-throttle-{uuid.uuid4().hex[:8]}@alkera.dev"
    await _signup(client, email)  # signup already sent one verification email
    assert len(monkeypatch_verification_send) == 1

    blocked = await client.post("/api/v1/auth/verify-email/resend")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert len(monkeypatch_verification_send) == 1  # nothing left the building
    # The refusal says when to come back, so clients can render a countdown.
    retry_after = int(blocked.headers["Retry-After"])
    assert 1 <= retry_after <= settings.email_verification_resend_cooldown_seconds

    # The verification window is deliberately LONGER than the 60s password-link
    # cooldown: two minutes in, a resend is still refused.
    await _age_pending_tokens(email, seconds=120)
    assert (await client.post("/api/v1/auth/verify-email/resend")).status_code == 429
    assert len(monkeypatch_verification_send) == 1

    # Past the full window the resend works again and rotates the token.
    await _age_pending_tokens(email, seconds=_PAST_VERIFICATION_COOLDOWN - 120)
    ok = await client.post("/api/v1/auth/verify-email/resend")
    assert ok.status_code == 200
    assert len(monkeypatch_verification_send) == 2
    assert monkeypatch_verification_send[0]["token"] != monkeypatch_verification_send[1]["token"]


async def test_verification_resend_cooldown_is_per_account(
    client: AsyncClient, monkeypatch_verification_send: list[dict], monkeypatch_welcome_send
) -> None:
    """The asymmetric case: one account's cooldown must not silence anyone
    else's verification email."""
    first = f"resend-a-{uuid.uuid4().hex[:8]}@alkera.dev"
    await _signup(client, first)
    assert (await client.post("/api/v1/auth/verify-email/resend")).status_code == 429

    second = f"resend-b-{uuid.uuid4().hex[:8]}@alkera.dev"
    await _signup(client, second)  # signing up logs the client in as `second`
    sent_before = len(monkeypatch_verification_send)
    await _age_pending_tokens(second, seconds=_PAST_VERIFICATION_COOLDOWN)
    assert (await client.post("/api/v1/auth/verify-email/resend")).status_code == 200
    assert len(monkeypatch_verification_send) == sent_before + 1
    assert monkeypatch_verification_send[-1]["email"] == second


async def test_password_link_route_is_refused_inside_the_cooldown(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.post("/api/v1/auth/password/send-reset")).status_code == 200
    assert len(monkeypatch_password_reset_send) == 1

    blocked = await client.post("/api/v1/auth/password/send-reset")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert len(monkeypatch_password_reset_send) == 1

    await _age_pending_tokens(org_admin.admin_email, seconds=120)
    assert (await client.post("/api/v1/auth/password/send-reset")).status_code == 200
    assert len(monkeypatch_password_reset_send) == 2


async def test_public_reset_request_stays_silent_and_constant_inside_the_cooldown(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict]
) -> None:
    """The public route must not grow an enumeration oracle to gain a throttle:
    a throttled request looks exactly like an unknown-address one."""
    body = {"email": org_admin.admin_email}
    first = await client.post("/api/v1/auth/password-reset/request", json=body)
    assert first.status_code == 200
    assert len(monkeypatch_password_reset_send) == 1
    issued = monkeypatch_password_reset_send[0]["token"]

    for _ in range(5):
        again = await client.post("/api/v1/auth/password-reset/request", json=body)
        assert again.status_code == 200
        assert again.json() == first.json()
    assert len(monkeypatch_password_reset_send) == 1  # the flood sent nothing

    unknown = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": f"nobody-{uuid.uuid4().hex[:8]}@alkera.dev"},
    )
    assert unknown.status_code == 200
    assert unknown.json() == first.json()

    # The link already in the user's inbox is untouched by the throttled retries.
    assert (
        await client.post(
            f"/api/v1/auth/password-reset/{issued}", json={"password": "brand-new-pass-123"}
        )
    ).status_code == 200


async def test_an_account_with_no_pending_token_is_never_throttled(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_verification_send: list
) -> None:
    """A user who has never been sent a link (no pending token) must get the
    first one immediately — the cooldown is a gap between sends, not a gate."""
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    assert pw is not None
    await login(client, member.email, pw)

    resp = await client.post("/api/v1/auth/verify-email/resend")
    assert resp.status_code == 200
    assert monkeypatch_verification_send[-1]["email"] == member.email


async def test_verified_account_short_circuits_before_the_cooldown(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_verification_send: list[dict]
) -> None:
    """An already-verified caller gets the existing no-op answer, not a 429."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as s:
        user = (
            await s.execute(select(User).where(User.email == org_admin.admin_email))
        ).scalar_one()
        user.email_verification_expires_at = datetime.now(UTC) + timedelta(days=2)
        await s.commit()

    resp = await client.post("/api/v1/auth/verify-email/resend")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Email already verified"
    assert monkeypatch_verification_send == []


async def test_me_exposes_resend_available_at_while_a_token_is_pending(
    client: AsyncClient, monkeypatch_verification_send: list[dict], monkeypatch_welcome_send
) -> None:
    """Clients render the resend countdown from /auth/me, so the instant must be
    issue-time + the verification cooldown — not guessed locally from a 429."""
    email = f"resend-me-{uuid.uuid4().hex[:8]}@alkera.dev"
    before = datetime.now(UTC)
    await _signup(client, email)  # signup sent the first email = started the window
    after = datetime.now(UTC)

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    raw = me.json()["verification_resend_available_at"]
    assert raw is not None
    available_at = datetime.fromisoformat(raw)
    cooldown = timedelta(seconds=settings.email_verification_resend_cooldown_seconds)
    assert before + cooldown <= available_at <= after + cooldown


async def test_me_resend_available_at_is_null_when_verified_or_no_token(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    # Verified admin → no countdown to render.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["verification_resend_available_at"] is None

    # Unverified member with no pending token → a resend is allowed immediately,
    # so there is likewise nothing to count down.
    member, pw = await make_member(real_session, org_id=org_admin.org_id)
    assert pw is not None
    await login(client, member.email, pw)
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["verification_resend_available_at"] is None
