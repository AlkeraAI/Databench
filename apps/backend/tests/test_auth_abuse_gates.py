"""Signup-time abuse gates: the disposable-domain refusal and the IP
forensics stamps (signup + last login)."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


def _signup_body(email: str) -> dict[str, str]:
    return {
        "email": email,
        "first_name": "Abuse",
        "last_name": "Gate",
        "password": "vaultkey-123",
        "org_name": f"GateCo {uuid.uuid4().hex[:6]}",
    }


async def test_disposable_domain_signup_is_refused(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json=_signup_body(f"farm-{uuid.uuid4().hex[:8]}@mailinator.com"),
    )
    assert resp.status_code == 400
    detail = resp.json()["error"]
    assert detail["code"] == "disposable_email_blocked"
    # Nothing was sent and no account exists.
    assert monkeypatch_verification_send == []


async def test_disposable_gate_has_no_personal_email_escape_hatch(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    # The asymmetric case: `allow_personal_email` bypasses the business-email
    # nudge, but must NOT bypass the disposable blocklist.
    body = _signup_body(f"farm-{uuid.uuid4().hex[:8]}@mailinator.com")
    body["allow_personal_email"] = True  # type: ignore[assignment]
    resp = await client.post("/api/v1/auth/signup", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "disposable_email_blocked"


async def test_signup_and_login_stamp_client_ips(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    email = f"ip-stamp-{uuid.uuid4().hex[:8]}@alkera.dev"
    body = _signup_body(email)
    resp = await client.post(
        "/api/v1/auth/signup", json=body, headers={"x-forwarded-for": "203.0.113.50"}
    )
    assert resp.status_code == 201

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        # The proxy-attested hop; signup seeds BOTH stamps (it is a login).
        assert user.signup_ip == "203.0.113.50"
        assert user.last_login_ip == "203.0.113.50"

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": body["password"]},
        headers={"x-forwarded-for": "198.51.100.99"},
    )
    assert resp.status_code == 200

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.signup_ip == "203.0.113.50"  # immutable after signup
        assert user.last_login_ip == "198.51.100.99"  # follows the latest login


async def test_forged_left_hops_cannot_choose_the_recorded_ip(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    """A caller can invent any LEFT-side X-Forwarded-For hops, but the trusted
    reverse proxy APPENDS the peer it actually saw — the forensics stamp must
    read the proxy-attested hop, not the attacker-chosen one, or an abuser
    points the investigation at someone else's address."""
    email = f"forged-xff-{uuid.uuid4().hex[:8]}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json=_signup_body(email),
        # "6.6.6.6" is what the attacker sent; "203.0.113.77" is what the
        # proxy appended after actually seeing them.
        headers={"x-forwarded-for": "6.6.6.6, 203.0.113.77"},
    )
    assert resp.status_code == 201

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.signup_ip == "203.0.113.77"


async def test_short_chain_records_nothing_under_multi_hop_trust(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_verification_send,
    monkeypatch_welcome_send,
) -> None:
    """With a two-hop trusted edge configured, a header carrying fewer hops
    than the chain appends means the chain was bypassed — the caller-controlled
    remnant must NOT be recorded (the hole would be: connect past the CDN with
    a forged single hop and choose your recorded IP)."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "forwarded_for_trusted_hops", 2)

    email = f"short-chain-{uuid.uuid4().hex[:8]}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json=_signup_body(email),
        headers={"x-forwarded-for": "6.6.6.6"},  # one hop where the chain appends two
    )
    assert resp.status_code == 201
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.signup_ip is None

    # The full-length chain still attests normally: two trailing hops were
    # appended by our proxies, so hops[-2] is the genuine client.
    email2 = f"full-chain-{uuid.uuid4().hex[:8]}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json=_signup_body(email2),
        headers={"x-forwarded-for": "6.6.6.6, 203.0.113.88, 10.0.0.8"},
    )
    assert resp.status_code == 201
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email2))).scalar_one()
        assert user.signup_ip == "203.0.113.88"


async def test_hostile_forwarded_header_never_breaks_auth(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    """X-Forwarded-For is attacker-controlled text: garbage (including strings
    past the 45-char column bound) must stamp NULL, never 500 the signup or
    login it rides on."""
    email = f"hostile-xff-{uuid.uuid4().hex[:8]}@alkera.dev"
    body = _signup_body(email)
    hostile = "A" * 300 + "'; DROP TABLE users; --"
    resp = await client.post("/api/v1/auth/signup", json=body, headers={"x-forwarded-for": hostile})
    assert resp.status_code == 201  # the signup itself is untouched

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.signup_ip is None
        assert user.last_login_ip is None

    # A later VALID login records its IP; a later hostile one must NOT erase it
    # — the spoofable header would otherwise be an erase-my-trail primitive.
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": body["password"]},
        headers={"x-forwarded-for": "2001:db8::7"},
    )
    assert ok.status_code == 200
    hostile_again = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": body["password"]},
        headers={"x-forwarded-for": "not-an-ip"},
    )
    assert hostile_again.status_code == 200

    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.last_login_ip == "2001:db8::7"  # the last KNOWN IP survives
