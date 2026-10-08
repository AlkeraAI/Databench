"""GitHub sign-in — plain OAuth2 (GitHub is not an OIDC provider).

Flow: exchange code → access token, then `GET /user` for the account id + name,
and `GET /user/emails` (a GitHub account can have several addresses) to pick the
sign-in email. We prefer a **verified business** address when one exists — a
user whose primary is personal (e.g. gmail) but who also has a work address on
the account then signs up with the work identity and skips the business-email
gate — while preserving the **verified-first** invariant the decision tree
relies on (it refuses to auto-link an unverified provider email). GitHub's
synthetic `*.noreply.github.com` alias is never preferred. `email_verified`
reflects the chosen entry. GitHub gives a single `name`; we split it for prefill
and let the user edit.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from alkera_core.http import async_client
from alkera_core.utils.email import email_domain

from backend.auth.email_policy import is_personal_email
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.profile import FederatedProfile

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 - public endpoint, not a secret
_USER_URL = "https://api.github.com/user"
_EMAILS_URL = "https://api.github.com/user/emails"
_HTTP_TIMEOUT = 10.0


def _split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _is_noreply_email(addr: str) -> bool:
    """GitHub's synthetic `*.noreply.github.com` alias — non-personal, but not a
    real deliverable org domain, so it must never be *preferred* over an actual
    address (it's only an absolute last resort)."""
    domain = email_domain(addr)
    return domain == "noreply.github.com" or domain.endswith(".noreply.github.com")


def _primary_first(addrs: list[tuple[str, bool, bool]]) -> list[tuple[str, bool, bool]]:
    """Reorder (email, verified, primary) tuples so the primary address (at most
    one per GitHub account) leads; otherwise preserve the API's order."""
    return [t for t in addrs if t[2]] + [t for t in addrs if not t[2]]


class GitHubProvider:
    key = "github"
    kind = "oauth2"

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str | None) -> str:
        # GitHub has no OIDC nonce; CSRF protection is the `state` param.
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
            "allow_signup": "true",
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def fetch_profile(
        self, *, code: str, redirect_uri: str, nonce: str | None
    ) -> FederatedProfile:
        access_token = await self._exchange(code=code, redirect_uri=redirect_uri)
        async with async_client(
            timeout=_HTTP_TIMEOUT,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            user = await self._get_json(client, _USER_URL)
            emails = await self._get_json(client, _EMAILS_URL)

        email, verified = self._pick_email(emails, fallback=user.get("email"))
        first, last = _split_name(str(user.get("name") or ""))
        return FederatedProfile(
            provider=self.key,
            subject=str(user["id"]),
            email=email,
            email_verified=verified,
            first_name=first,
            last_name=last,
            raw={"user": user, "emails": emails},
        )

    async def _exchange(self, *, code: str, redirect_uri: str) -> str:
        try:
            async with async_client(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"github: token exchange failed: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise OAuthError(f"github: token exchange returned no access_token ({payload!r})")
        return str(token)

    @staticmethod
    async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError(f"github: GET {url} failed: {exc}") from exc

    @staticmethod
    def _pick_email(emails: Any, *, fallback: str | None) -> tuple[str, bool]:
        """Choose an email, preferring a verified business address.

        Priority (first match wins), preserving the verified-first invariant the
        decision tree relies on (it refuses to auto-link an unverified email):
          1. verified business      4. unverified business
          2. verified (any real)    5. unverified (any real)
          3. (real before noreply)  6. `*.noreply.github.com` alias / fallback
        Within each tier the primary address leads. Returns (email, verified);
        a non-verified result blocks auto-link.
        """
        if isinstance(emails, list):
            addrs = [
                (str(e["email"]).lower(), bool(e.get("verified")), bool(e.get("primary")))
                for e in emails
                if e.get("email")
            ]
            ordered = _primary_first(addrs)
            # Real addresses (anything but the synthetic noreply alias) win over
            # noreply; within that, verified beats unverified, business beats
            # personal.
            real = [(a, v) for (a, v, _p) in ordered if not _is_noreply_email(a)]
            for want_verified in (True, False):
                tier = [a for (a, v) in real if v is want_verified]
                business = [a for a in tier if not is_personal_email(a)]
                if business:
                    return business[0], want_verified
                if tier:
                    return tier[0], want_verified
            # Only the noreply alias remained — an absolute last resort.
            for a, v, _p in ordered:
                if _is_noreply_email(a):
                    return a, v
        if fallback:
            return str(fallback).lower(), False
        raise OAuthError("github: account has no usable email address")
