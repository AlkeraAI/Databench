"""Cloudflare Turnstile verification — verifier unit + route enforcement.

The verifier (`backend.auth.captcha.verify_turnstile`) is driven over an injected
`httpx.MockTransport`, so these are free + deterministic (no network, no key). The
route tests monkeypatch the verifier to prove the public auth endpoints CALL it and
propagate its rejection; a separate case proves the endpoints are unaffected when
Turnstile is unconfigured (the default for dev + this suite).
"""

from __future__ import annotations

import secrets

import httpx
import pytest
from alkera_core.config import settings
from backend.auth import captcha
from fastapi import HTTPException
from httpx import AsyncClient


def _mock_client(handler) -> AsyncClient:  # type: ignore[no-untyped-def]
    return AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def enable_turnstile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the seam on by giving it a secret (no real key needed — the transport
    is mocked) AND the dev opt-in flag, since the test env is non-production where a
    key alone leaves the captcha off."""
    monkeypatch.setattr(settings, "turnstile_secret_key", "test-secret")
    monkeypatch.setattr(settings, "turnstile_dev_enabled", True)


# --------------------------------------------------------------------------- #
# Verifier unit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "any-token"], ids=["none", "empty", "present"])
async def test_unconfigured_is_a_noop(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    # With no secret set the check never runs — even a missing token passes, and no
    # HTTP call is made (a None client would blow up if it tried to use one).
    monkeypatch.setattr(settings, "turnstile_secret_key", None)
    await captcha.verify_turnstile(token)


@pytest.mark.asyncio
async def test_success_passes(enable_turnstile: None) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"success": True, "hostname": "app.example.com"})

    await captcha.verify_turnstile("good", client=_mock_client(handler))
    # The token + secret are posted form-encoded to Cloudflare.
    assert "secret=test-secret" in seen["body"]
    assert "response=good" in seen["body"]


@pytest.mark.asyncio
async def test_explicit_failure_is_rejected(enable_turnstile: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )

    with pytest.raises(HTTPException) as exc:
        await captcha.verify_turnstile("bad", client=_mock_client(handler))
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "captcha_failed"


@pytest.mark.asyncio
async def test_missing_token_when_enabled_is_rejected(enable_turnstile: None) -> None:
    # Enabled but the client sent nothing — reject without any HTTP call.
    with pytest.raises(HTTPException) as exc:
        await captcha.verify_turnstile(None)
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "captcha_failed"


@pytest.mark.asyncio
async def test_transport_error_fails_open(enable_turnstile: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cloudflare unreachable")

    # Fail OPEN: a Cloudflare outage must not lock users out — no exception raised.
    await captcha.verify_turnstile("good", client=_mock_client(handler))


@pytest.mark.asyncio
async def test_http_5xx_fails_open(enable_turnstile: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    # raise_for_status → HTTPStatusError (an httpx.HTTPError) → fail open.
    await captcha.verify_turnstile("good", client=_mock_client(handler))


@pytest.mark.asyncio
async def test_remote_ip_is_forwarded(enable_turnstile: None) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"success": True})

    await captcha.verify_turnstile("good", remote_ip="203.0.113.7", client=_mock_client(handler))
    assert "remoteip=203.0.113.7" in seen["body"]


# --------------------------------------------------------------------------- #
# Route enforcement
# --------------------------------------------------------------------------- #


@pytest.fixture
def require_captcha_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the auth routes to require `turnstile_token == "good-token"`.

    Monkeypatches the verifier where the routes call it, so we test wiring (the
    route invokes it + propagates the 400) without any real Cloudflare call."""

    async def _fake(token: str | None, *, remote_ip: str | None = None) -> None:
        if token != "good-token":
            raise HTTPException(
                status_code=400, detail={"code": "captcha_failed", "message": "nope"}
            )

    monkeypatch.setattr("backend.api.routes.identity.auth.verify_turnstile", _fake)


def _signup_body(**extra: object) -> dict[str, object]:
    return {
        "email": f"captcha-{secrets.token_hex(6)}@alkera.dev",
        "first_name": "Cap",
        "last_name": "Tcha",
        "password": "vaultkey-24680",
        "org_name": f"Cap Org {secrets.token_hex(3)}",
        **extra,
    }


@pytest.mark.asyncio
async def test_login_rejected_without_captcha(client, org_admin, require_captcha_token) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": org_admin.admin_password},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "captcha_failed"


@pytest.mark.asyncio
async def test_login_succeeds_with_captcha(client, org_admin, require_captcha_token) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": org_admin.admin_email,
            "password": org_admin.admin_password,
            "turnstile_token": "good-token",
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_unaffected_when_turnstile_unconfigured(
    client, org_admin, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    # The REAL verifier runs; with no secret it's a no-op even though no token is
    # sent. Force-unset so the test is hermetic regardless of a dev's .env.local.
    # This is the regression guard that the default suite stays captcha-free.
    monkeypatch.setattr(settings, "turnstile_secret_key", None)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": org_admin.admin_password},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_signup_rejected_without_captcha(
    client, require_captcha_token, monkeypatch_verification_send
) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post("/api/v1/auth/signup", json=_signup_body())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "captcha_failed"
    # Captcha runs first → no account created, no verification email sent.
    assert monkeypatch_verification_send == []


@pytest.mark.asyncio
async def test_signup_succeeds_with_captcha(
    client, require_captcha_token, monkeypatch_verification_send
) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post("/api/v1/auth/signup", json=_signup_body(turnstile_token="good-token"))
    assert resp.status_code == 201
    assert len(monkeypatch_verification_send) == 1


@pytest.mark.asyncio
async def test_password_reset_rejected_without_captcha(
    client, require_captcha_token, monkeypatch_password_reset_send
) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": "nobody@alkera.dev"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "captcha_failed"
    # Captcha runs first → no reset issued/sent.
    assert monkeypatch_password_reset_send == []


@pytest.mark.asyncio
async def test_password_reset_succeeds_with_captcha(
    client, org_admin, require_captcha_token, monkeypatch_password_reset_send
) -> None:  # type: ignore[no-untyped-def]
    # A good token lets the request through to the (real) reset flow for a known
    # account — proving captcha gates BEFORE the work, not that it blocks it.
    resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": org_admin.admin_email, "turnstile_token": "good-token"},
    )
    assert resp.status_code == 200
    assert len(monkeypatch_password_reset_send) == 1


@pytest.mark.asyncio
async def test_password_reset_unaffected_when_turnstile_unconfigured(
    client, org_admin, monkeypatch_password_reset_send, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    # The REAL verifier runs; force-unset so the test is hermetic regardless of a
    # dev's .env.local. Regression guard that the default suite stays captcha-free.
    monkeypatch.setattr(settings, "turnstile_secret_key", None)
    resp = await client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": org_admin.admin_email},
    )
    assert resp.status_code == 200
    assert len(monkeypatch_password_reset_send) == 1
