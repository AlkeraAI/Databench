"""Mock provider — drives the entire OAuth flow with zero external calls.

Enabled only when ``settings.oauth_mock_enabled`` (forced off in production).
Two uses:

* **Tests** build a profile, encode it with :meth:`MockProvider.encode_code`,
  and hit the callback directly — every decision-tree branch, deterministically.
* **Local click-through**: ``authorization_url`` points at a backend-rendered
  mock "consent" page (see the ``/oauth/mock/authorize`` route) where a dev
  types any email/name and gets bounced back to the callback — so the real SPA
  flow can be exercised without Google/GitHub credentials.

The ``code`` is just a base64url-encoded JSON ``FederatedProfile`` — there is no
secret here; the mock is a stand-in for a real provider, not an auth control.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlencode

from alkera_core.config import settings

from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.profile import FederatedProfile

MOCK_AUTHORIZE_PATH = "/api/v1/auth/oauth/mock/authorize"


class MockProvider:
    key = "mock"
    kind = "mock"

    @staticmethod
    def encode_code(profile: FederatedProfile) -> str:
        payload = {
            "subject": profile.subject,
            "email": profile.email,
            "email_verified": profile.email_verified,
            "first_name": profile.first_name,
            "last_name": profile.last_name,
        }
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def decode_code(code: str) -> FederatedProfile:
        try:
            raw = base64.urlsafe_b64decode(code.encode("ascii"))
            data = json.loads(raw)
            return FederatedProfile(
                provider="mock",
                subject=str(data["subject"]),
                email=str(data["email"]).lower(),
                email_verified=bool(data.get("email_verified", False)),
                first_name=str(data.get("first_name") or ""),
                last_name=str(data.get("last_name") or ""),
                raw=dict(data),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise OAuthError(f"mock: bad code: {exc}") from exc

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str | None) -> str:
        params = {"state": state, "redirect_uri": redirect_uri}
        base = settings.oauth_redirect_base
        return f"{base}{MOCK_AUTHORIZE_PATH}?{urlencode(params)}"

    async def fetch_profile(
        self, *, code: str, redirect_uri: str, nonce: str | None
    ) -> FederatedProfile:
        return self.decode_code(code)
