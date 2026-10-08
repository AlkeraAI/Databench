"""Cloudflare Turnstile verification for the public auth endpoints.

The SPA attaches an invisible Turnstile token to login / signup / password-reset;
`verify_turnstile` checks it server-side against Cloudflare's siteverify endpoint
before any account work happens.

Two deliberate behaviours, mirroring the rest of the auth/email layer:

- **No-op when unconfigured.** With no `TURNSTILE_SECRET_KEY` set, the check
  returns immediately — so local dev and the default (free, key-less) test suite
  run without a captcha. Production requires the key (see `_validate_production`).
- **Fail-OPEN on an outage, fail-CLOSED on a rejection.** A network/timeout error
  reaching Cloudflare is logged and allowed through: a Cloudflare outage must not
  lock every user out of login/signup. An explicit `success: false` from
  Cloudflare (bad/missing/replayed token) IS rejected.

Tests drive it with an injected `httpx.AsyncClient` over a `MockTransport`
(see `apps/backend/tests/test_captcha.py`) — the same seam the CLI plugins use.
"""

from __future__ import annotations

from typing import Any

import httpx
from alkera_core.config import settings
from alkera_core.http import async_client
from alkera_core.logging import get_logger
from fastapi import HTTPException, status

log = get_logger(__name__)

_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_HTTP_TIMEOUT = 10.0

# Structured detail so the SPA can distinguish a captcha failure from a generic
# 400 (same convention as the personal-email gate in the signup route).
_CAPTCHA_FAILED_DETAIL = {
    "code": "captcha_failed",
    "message": "Captcha verification failed. Please try again.",
}


def _reject() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_CAPTCHA_FAILED_DETAIL)


async def verify_turnstile(
    token: str | None,
    *,
    remote_ip: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Verify a Cloudflare Turnstile token. Raise HTTP 400 (`captcha_failed`) on a
    failed challenge; return on success.

    No-op when `TURNSTILE_SECRET_KEY` is unset. Fail-open on a transport error,
    fail-closed on an explicit `success: false`. Pass `client` (an
    `httpx.AsyncClient` over a `MockTransport`) in tests; production builds one.
    """
    if not settings.turnstile_enabled:
        return
    if not token:
        # Enabled but the SPA sent no token — a bot hitting the API directly, or a
        # broken/missing widget. Reject before any account work.
        raise _reject()

    data = {"secret": settings.turnstile_secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    owns_client = client is None
    client = client or async_client(timeout=_HTTP_TIMEOUT)
    try:
        resp = await client.post(_SITEVERIFY_URL, data=data)
        resp.raise_for_status()
        body: dict[str, Any] = dict(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        # Fail OPEN: a Cloudflare outage must not take down login/signup.
        log.warning("captcha.verify_failed", error=str(exc))
        return
    finally:
        if owns_client:
            await client.aclose()

    if not body.get("success", False):
        log.warning("captcha.rejected", error_codes=body.get("error-codes"))
        raise _reject()
    log.info("captcha.verified", hostname=body.get("hostname"))


__all__ = ["verify_turnstile"]
