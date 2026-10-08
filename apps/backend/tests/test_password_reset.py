"""Password reset routes — request, consume, expiry, enumeration safety."""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select


@pytest.fixture
def no_email_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the per-account outbound-email cooldown, for tests that deliberately
    re-request back-to-back. The cooldown itself is pinned in
    test_auth_email_throttle.py."""
    monkeypatch.setattr("backend.api.routes.identity.auth._EMAIL_RESEND_COOLDOWN", timedelta(0))


async def _signup(client: AsyncClient, email: str, password: str, suffix: str) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Reset",
            "last_name": "Tester",
            "password": password,
            "org_name": f"ResetCo {suffix}",
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_request_for_unknown_email_is_silent(
    client: AsyncClient, monkeypatch_password_reset_send
):
    """Unknown email still returns 200 — we deliberately don't leak existence."""
    resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": f"nobody-{secrets.token_hex(4)}@alkera.dev"},
    )
    assert resp.status_code == 200
    # No email queued because the user doesn't exist.
    assert monkeypatch_password_reset_send == []


@pytest.mark.asyncio
async def test_request_for_known_email_issues_token(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    suffix = secrets.token_hex(4)
    email = f"resetme-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)

    resp = await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    assert resp.status_code == 200
    assert len(monkeypatch_password_reset_send) == 1
    sent = monkeypatch_password_reset_send[0]
    assert sent["email"] == email
    assert isinstance(sent["token"], str) and len(sent["token"]) > 16


@pytest.mark.asyncio
async def test_consume_token_updates_password(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    suffix = secrets.token_hex(4)
    email = f"reset-flow-{suffix}@alkera.dev"
    old_password = "old-password-12345"
    new_password = "new-password-67890"
    await _signup(client, email, old_password, suffix)
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = monkeypatch_password_reset_send[-1]["token"]

    resp = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": new_password}
    )
    assert resp.status_code == 200

    # Old password no longer works.
    bad = await client.post("/api/v1/auth/login", json={"email": email, "password": old_password})
    assert bad.status_code == 401

    # New password works.
    good = await client.post("/api/v1/auth/login", json={"email": email, "password": new_password})
    assert good.status_code == 200

    # Reusing the same token now fails.
    again = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": "another-pass-123"}
    )
    assert again.status_code == 400


@pytest.mark.asyncio
async def test_consume_unknown_token_400(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/password-reset/nope-not-a-real-token",
        json={"password": "whatever-12345"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_consume_expired_token_400(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    suffix = secrets.token_hex(4)
    email = f"reset-expired-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = monkeypatch_password_reset_send[-1]["token"]

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.password_reset_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    resp = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": "fresh-pass-12345"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_re_request_invalidates_prior_token(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
    no_email_cooldown,
):
    suffix = secrets.token_hex(4)
    email = f"reset-rotate-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)

    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    first_token = monkeypatch_password_reset_send[-1]["token"]

    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    second_token = monkeypatch_password_reset_send[-1]["token"]
    assert first_token != second_token

    bad = await client.post(
        f"/api/v1/auth/password-reset/{first_token}",
        json={"password": "after-rotate-12345"},
    )
    assert bad.status_code == 400

    good = await client.post(
        f"/api/v1/auth/password-reset/{second_token}",
        json={"password": "after-rotate-12345"},
    )
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_password_too_short_rejected(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    suffix = secrets.token_hex(4)
    email = f"reset-short-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = monkeypatch_password_reset_send[-1]["token"]

    resp = await client.post(f"/api/v1/auth/password-reset/{token}", json={"password": "short"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reset_token_stored_hashed_not_plaintext(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    """The DB stores only the HMAC hash; the raw (emailed) token still consumes,
    and presenting the stored hash as the token must NOT work."""
    from alkera_core.auth import hash_lookup_token

    suffix = secrets.token_hex(4)
    email = f"reset-hashed-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    raw_token = monkeypatch_password_reset_send[-1]["token"]

    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        stored = user.password_reset_token

    assert stored is not None
    assert stored != raw_token
    assert stored == hash_lookup_token(raw_token)

    # Presenting the STORED hash as the token fails (double-hash → no match).
    bad = await client.post(
        f"/api/v1/auth/password-reset/{stored}", json={"password": "should-not-work-123"}
    )
    assert bad.status_code == 400

    # The raw token still consumes (hashed lookup round-trips).
    good = await client.post(
        f"/api/v1/auth/password-reset/{raw_token}", json={"password": "brand-new-67890"}
    )
    assert good.status_code == 200


# --- the answer does not wait on the relay ----------------------------------
#
# Awaiting the SMTP round-trip only for a known address made the response time
# say what the constant body hides. Each case holds the send open on an event
# the test controls, so "answered before the send finished" is an ordering
# fact, not a wall-clock measurement.

_REQUEST = "/api/v1/auth/password-reset/request"
#: Bounds a request that would otherwise wait forever on the held send.
_ANSWER_WITHIN = 10.0


class _HeldSend:
    """A stand-in sender that does not return until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[str] = []
        self.finished: list[tuple[str, str]] = []

    async def __call__(self, user: User, *, token: str) -> None:
        self.started.append(user.email)
        await self.release.wait()
        self.finished.append((user.email, token))


@pytest.mark.asyncio
async def test_known_and_unknown_addresses_answer_before_any_send_finishes(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_verification_send,
) -> None:
    from alkera_core.email import drain_background_sends

    suffix = secrets.token_hex(4)
    known = f"held-{suffix}@alkera.dev"
    await _signup(client, known, "old-password-12345", suffix)
    held = _HeldSend()
    monkeypatch.setattr("backend.api.routes.identity.auth.send_password_reset", held)

    known_resp = await asyncio.wait_for(
        client.post(_REQUEST, json={"email": known}), timeout=_ANSWER_WITHIN
    )
    unknown_resp = await asyncio.wait_for(
        client.post(_REQUEST, json={"email": f"nobody-{suffix}@alkera.dev"}),
        timeout=_ANSWER_WITHIN,
    )

    # Both answered while the known address's send was still held open.
    assert held.finished == []
    assert known_resp.status_code == unknown_resp.status_code == 200
    assert known_resp.content == unknown_resp.content

    held.release.set()
    await drain_background_sends()
    # Exactly one send, for the known address only, carrying a real token.
    assert held.started == [known]
    assert len(held.finished) == 1
    email, token = held.finished[0]
    assert email == known
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == known))).scalar_one()
    from alkera_core.auth.token_hash import hash_lookup_token

    assert user.password_reset_token == hash_lookup_token(token)


@pytest.mark.asyncio
async def test_the_background_reset_email_reaches_the_relay_with_the_live_link(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_verification_send,
) -> None:
    """The real sender, run after the request's session has closed, still builds
    the message for the right address with the token the database holds."""
    from alkera_core.auth.token_hash import hash_lookup_token
    from alkera_core.config import settings
    from alkera_core.email import drain_background_sends

    suffix = secrets.token_hex(4)
    email = f"relay-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)

    release = asyncio.Event()
    delivered: list[EmailMessage] = []

    async def _relay(message: EmailMessage, **_kwargs: object) -> None:
        await release.wait()
        delivered.append(message)

    monkeypatch.setattr(settings, "email_enabled", True)
    monkeypatch.setattr("alkera_core.email.aiosmtplib.send", _relay)

    resp = await asyncio.wait_for(client.post(_REQUEST, json={"email": email}), _ANSWER_WITHIN)
    assert resp.status_code == 200
    assert delivered == []

    release.set()
    await drain_background_sends()
    assert len(delivered) == 1
    message = delivered[0]
    assert message["To"] == email
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    link = re.search(r"/reset-password/(\S+)", body.get_content())
    assert link is not None
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
    assert user.password_reset_token == hash_lookup_token(link.group(1))


@pytest.mark.asyncio
async def test_a_failing_background_send_is_logged_and_never_reaches_the_caller(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_verification_send,
) -> None:
    from alkera_core.email import drain_background_sends
    from structlog.testing import capture_logs

    suffix = secrets.token_hex(4)
    email = f"boom-{suffix}@alkera.dev"
    await _signup(client, email, "old-password-12345", suffix)

    async def _explode(user: User, *, token: str) -> None:
        raise RuntimeError("relay exploded")

    monkeypatch.setattr("backend.api.routes.identity.auth.send_password_reset", _explode)
    with capture_logs() as records:
        resp = await client.post(_REQUEST, json={"email": email})
        await drain_background_sends()
    assert resp.status_code == 200
    failures = [r for r in records if r["event"] == "email.background_send.failed"]
    assert len(failures) == 1
    assert failures[0]["error"] == "relay exploded"


@pytest.mark.asyncio
async def test_reset_clears_the_login_lockout(
    client: AsyncClient,
    monkeypatch_verification_send,
    monkeypatch_password_reset_send,
):
    """A completed reset lets the user sign in at once, even if the account was
    locked by failed attempts: the success page promises it, and the lockout
    otherwise only clears on a login the locked user cannot make."""
    from alkera_core.config import settings

    suffix = secrets.token_hex(4)
    email = f"reset-lock-{suffix}@alkera.dev"
    old_password = "old-password-12345"
    new_password = "new-password-67890"
    await _signup(client, email, old_password, suffix)

    # Lock the account: threshold+1 wrong passwords.
    for _ in range(settings.auth_lockout_threshold + 1):
        await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pass-x"})
    locked = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": old_password}
    )
    assert locked.status_code == 429, locked.text

    # Reset the password.
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = monkeypatch_password_reset_send[-1]["token"]
    resp = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": new_password}
    )
    assert resp.status_code == 200

    # The new password works immediately — the lock did not survive the reset.
    good = await client.post("/api/v1/auth/login", json={"email": email, "password": new_password})
    assert good.status_code == 200, good.text
