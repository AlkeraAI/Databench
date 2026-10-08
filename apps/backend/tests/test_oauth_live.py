"""LIVE OAuth tests against the REAL Google + GitHub apps.

These use the actual client id/secret from the environment (`.env.local`
locally; repo secrets in CI). They are grouped under the existing `opencode_e2e`
marker — the NON-paid e2e suite (`make e2e` / the `e2e-opencode` CI job) — so
they are excluded from the default `pytest` run and never gate the fast suite.
They make only free OAuth-infra calls (discovery / JWKS / a single rejected
token exchange); no LLM/paid APIs, so they do NOT belong in `live_provider`.

What they can and cannot verify:
  * CANNOT complete a full interactive login — the authorization-code grant
    needs a human at the provider's consent screen; there is no headless
    password grant for Google/GitHub web apps.
  * CAN verify everything automatable with just the app credentials:
      - OIDC discovery + JWKS are reachable (Google),
      - the authorize URL is built correctly with the real client_id + our
        registered redirect_uri,
      - the provider ACCEPTS our client credentials at the token endpoint — a
        bogus code yields `invalid_grant` / `bad_verification_code`, NOT a
        client-authentication error. That is the definitive "are these
        credentials valid + accepted by the provider" check.

Each test skips unless its provider's credentials are configured, so a missing
key degrades gracefully.
"""

from __future__ import annotations

import httpx
import pytest
from alkera_core.config import settings
from backend.auth.oauth.github import GitHubProvider
from backend.auth.oauth.google import google_provider

pytestmark = [pytest.mark.opencode_e2e, pytest.mark.asyncio]

requires_google = pytest.mark.skipif(
    not settings.google_oauth_configured, reason="Google OAuth credentials not configured"
)
requires_github = pytest.mark.skipif(
    not settings.github_oauth_configured, reason="GitHub OAuth credentials not configured"
)

_GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _redirect(provider: str) -> str:
    return f"{settings.oauth_redirect_base}/api/v1/auth/oauth/{provider}/callback"


# --- Google ----------------------------------------------------------------


@requires_google
async def test_google_discovery_and_authorize_url() -> None:
    assert settings.oauth_google_client_id is not None
    assert settings.oauth_google_client_secret is not None
    provider = google_provider(
        client_id=settings.oauth_google_client_id,
        client_secret=settings.oauth_google_client_secret,
    )
    # Hits the REAL Google discovery document to resolve the authorize endpoint.
    url = await provider.authorization_url(
        redirect_uri=_redirect("google"), state="state-xyz", nonce="nonce-abc"
    )
    assert url.startswith("https://accounts.google.com/")
    assert "response_type=code" in url
    assert "scope=openid" in url
    assert "state=state-xyz" in url
    assert "nonce=nonce-abc" in url
    assert settings.oauth_google_client_id in url


@requires_google
async def test_google_jwks_is_reachable() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        meta = (await client.get(_GOOGLE_DISCOVERY)).json()
        jwks = (await client.get(meta["jwks_uri"])).json()
    assert isinstance(jwks.get("keys"), list) and jwks["keys"], "Google JWKS should expose keys"


@requires_google
async def test_google_credentials_are_accepted_at_token_endpoint() -> None:
    """A bogus code with our REAL client creds must fail with `invalid_grant`
    (the code is bad) — NOT `invalid_client` (which would mean the credentials
    themselves are wrong). Proves Google accepts our client id + secret."""
    assert settings.oauth_google_client_id is not None
    async with httpx.AsyncClient(timeout=10.0) as client:
        meta = (await client.get(_GOOGLE_DISCOVERY)).json()
        resp = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": "definitely-not-a-real-code",
                "redirect_uri": _redirect("google"),
                "client_id": settings.oauth_google_client_id,
                "client_secret": settings.oauth_google_client_secret,
            },
            headers={"Accept": "application/json"},
        )
    body = resp.json()
    error = body.get("error")
    assert error not in {"invalid_client", "unauthorized_client"}, (
        f"Google REJECTED our client credentials — check OAUTH_GOOGLE_CLIENT_ID/SECRET: {body}"
    )
    assert error == "invalid_grant", (
        f"expected `invalid_grant` for a bogus code; got {error!r}. If this is "
        f"`redirect_uri_mismatch`, register {_redirect('google')!r} in the Google app. Full: {body}"
    )


# --- GitHub ----------------------------------------------------------------


@requires_github
async def test_github_authorize_url() -> None:
    assert settings.oauth_github_client_id is not None
    assert settings.oauth_github_client_secret is not None
    provider = GitHubProvider(
        client_id=settings.oauth_github_client_id,
        client_secret=settings.oauth_github_client_secret,
    )
    url = await provider.authorization_url(
        redirect_uri=_redirect("github"), state="state-xyz", nonce=None
    )
    assert url.startswith("https://github.com/login/oauth/authorize")
    assert "scope=read" in url  # read:user user:email (url-encoded)
    assert "state=state-xyz" in url
    assert settings.oauth_github_client_id in url


@requires_github
async def test_github_credentials_are_accepted_at_token_endpoint() -> None:
    """A bogus code with our REAL GitHub creds returns `bad_verification_code`
    (the code is bad) — NOT `incorrect_client_credentials` (bad creds). Proves
    GitHub accepts our client id + secret."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": settings.oauth_github_client_id,
                "client_secret": settings.oauth_github_client_secret,
                "code": "definitely-not-a-real-code",
                "redirect_uri": _redirect("github"),
            },
            headers={"Accept": "application/json"},
        )
    body = resp.json()
    error = body.get("error")
    assert error != "incorrect_client_credentials", (
        f"GitHub REJECTED our client credentials — check OAUTH_GITHUB_CLIENT_ID/SECRET: {body}"
    )
    assert error == "bad_verification_code", (
        f"expected `bad_verification_code` for a bogus code; got {error!r}. Full: {body}"
    )
