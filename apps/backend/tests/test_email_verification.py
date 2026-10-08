"""Email verification routes — issue, resend, consume."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select


@pytest.fixture
def no_email_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the verification-resend cooldown, for tests that deliberately
    resend back-to-back. The cooldown itself is pinned in
    test_auth_email_throttle.py."""
    monkeypatch.setattr(settings, "email_verification_resend_cooldown_seconds", 0)


@pytest.mark.asyncio
async def test_bare_signup_issues_verification_token(
    client: AsyncClient, monkeypatch_verification_send
):
    suffix = secrets.token_hex(4)
    email = f"newuser-{suffix}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "New",
            "last_name": "User",
            "password": "vaultkey-12345",
            "org_name": f"NewCo {suffix}",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["email_verified_at"] is None
    assert len(monkeypatch_verification_send) == 1
    sent = monkeypatch_verification_send[0]
    assert sent["email"] == email
    assert isinstance(sent["token"], str) and len(sent["token"]) > 16


@pytest.mark.asyncio
async def test_verify_email_consumes_token_and_clears_pending(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
):
    suffix = secrets.token_hex(4)
    email = f"toverify-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "ToVerify",
            "last_name": "User",
            "password": "vaultkey-12345-2",
            "org_name": f"VerifyCo {suffix}",
        },
    )
    token = monkeypatch_verification_send[-1]["token"]

    resp = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert resp.status_code == 200
    assert resp.json()["email_verified_at"] is not None

    # Reusing the token reports "already verified" — NOT the indistinguishable
    # 400 "token not found" the SPA would render as a broken link.
    again = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert again.status_code == 409, again.text
    assert "already verified" in again.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_verify_email_unknown_token_400(client: AsyncClient):
    resp = await client.post("/api/v1/auth/verify-email/nope-not-a-real-token")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_verify_email_expired_token(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
):
    suffix = secrets.token_hex(4)
    email = f"expired-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Expired",
            "last_name": "User",
            "password": "vaultkey-12345-3",
            "org_name": f"ExpiredCo {suffix}",
        },
    )
    token = monkeypatch_verification_send[-1]["token"]

    # Force the token to be expired in the DB.
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.email_verification_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    resp = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_resend_verification_requires_login(client: AsyncClient):
    resp = await client.post("/api/v1/auth/verify-email/resend")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_resend_verification_issues_new_token(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send, no_email_cooldown
):
    suffix = secrets.token_hex(4)
    email = f"resend-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Resend",
            "last_name": "User",
            "password": "vaultkey-12345-4",
            "org_name": f"ResendCo {suffix}",
        },
    )
    first_token = monkeypatch_verification_send[-1]["token"]

    resp = await client.post("/api/v1/auth/verify-email/resend")
    assert resp.status_code == 200
    second_token = monkeypatch_verification_send[-1]["token"]
    assert second_token != first_token

    # Old token is invalid; new one works.
    bad = await client.post(f"/api/v1/auth/verify-email/{first_token}")
    assert bad.status_code == 400
    good = await client.post(f"/api/v1/auth/verify-email/{second_token}")
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_resend_after_verification_is_noop(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
):
    suffix = secrets.token_hex(4)
    email = f"already-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Already",
            "last_name": "User",
            "password": "vaultkey-12345-5",
            "org_name": f"AlreadyCo {suffix}",
        },
    )
    token = monkeypatch_verification_send[-1]["token"]
    await client.post(f"/api/v1/auth/verify-email/{token}")

    sent_before = len(monkeypatch_verification_send)
    resp = await client.post("/api/v1/auth/verify-email/resend")
    assert resp.status_code == 200
    # No new email recorded.
    assert len(monkeypatch_verification_send) == sent_before


@pytest.mark.asyncio
async def test_verification_token_stored_hashed_not_plaintext(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
):
    """The DB stores only the HMAC hash; the raw (emailed) token still verifies,
    and presenting the stored hash must NOT verify."""
    from alkera_core.auth import hash_lookup_token

    suffix = secrets.token_hex(4)
    email = f"verif-hashed-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Verif",
            "last_name": "Hashed",
            "password": "vaultkey-12345-6",
            "org_name": f"VerifHash {suffix}",
        },
    )
    raw_token = monkeypatch_verification_send[-1]["token"]

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        stored = user.email_verification_token

    assert stored is not None
    assert stored != raw_token
    assert stored == hash_lookup_token(raw_token)

    # The stored hash, presented as a token, must not verify.
    bad = await client.post(f"/api/v1/auth/verify-email/{stored}")
    assert bad.status_code == 400
    # The raw token does.
    good = await client.post(f"/api/v1/auth/verify-email/{raw_token}")
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_verified_link_keeps_answering_already_verified(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
):
    """The emailed link stays friendly however many times it is opened.

    A verification link is clicked from an inbox, so it gets opened again — a
    second tab, a prefetching mail client, the SPA firing the request twice.
    Every one of those must read as "you're already verified" (the SPA renders
    409 as success); a 400 "Verification token not found" is indistinguishable
    from a forged link and shows the new user a broken first impression.
    """
    suffix = secrets.token_hex(4)
    email = f"replay-{suffix}@alkera.dev"
    await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Replay",
            "last_name": "User",
            "password": "vaultkey-12345-7",
            "org_name": f"ReplayCo {suffix}",
        },
    )
    token = monkeypatch_verification_send[-1]["token"]
    assert (await client.post(f"/api/v1/auth/verify-email/{token}")).status_code == 200

    for _ in range(3):
        again = await client.post(f"/api/v1/auth/verify-email/{token}")
        assert again.status_code == 409, again.text
        assert "already verified" in again.json()["error"]["message"].lower()

    # A token that was never issued is still a hard 400 — "already verified" is
    # only ever reported to the holder of a real link, so this can't be used to
    # probe which addresses exist.
    unknown = await client.post(f"/api/v1/auth/verify-email/{secrets.token_urlsafe(48)}")
    assert unknown.status_code == 400


@pytest.mark.asyncio
async def test_a_refused_resend_is_an_error_and_arms_no_cooldown(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, monkeypatch_welcome_send
):
    """The relay refusing the resend answers 503, not "Verification email sent",
    and leaves no cooldown behind: the very next resend goes out."""
    import aiosmtplib

    suffix = secrets.token_hex(4)
    email = f"refused-{suffix}@alkera.dev"
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "vaultkey-12345-8", "org_name": f"RefusedCo {suffix}"},
    )
    assert signup.status_code == 201, signup.text
    # Age the signup's token past the cooldown so only the refused resend could arm one.
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.email_verification_expires_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()

    async def _refuse(message, **kwargs):  # type: ignore[no-untyped-def]
        raise aiosmtplib.errors.SMTPException("relay down")

    monkeypatch.setattr(aiosmtplib, "send", _refuse)
    refused = await client.post("/api/v1/auth/verify-email/resend")
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "email_send_failed"

    delivered: list[str] = []

    async def _accept(message, **kwargs):  # type: ignore[no-untyped-def]
        delivered.append(message["To"])

    monkeypatch.setattr(aiosmtplib, "send", _accept)
    retried = await client.post("/api/v1/auth/verify-email/resend")
    assert retried.status_code == 200, retried.text
    assert delivered == [email]
