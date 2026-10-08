"""The provider contract.

Every login mechanism — OAuth2 (GitHub), OIDC (Google, future SSO), SAML
(future) — implements this. The route layer only ever calls these two methods
and only ever sees a `FederatedProfile`, so new mechanisms are additive.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.auth.oauth.profile import FederatedProfile


class OAuthError(Exception):
    """Provider-side failure (bad code, token exchange failed, id_token invalid).

    The route maps every instance to a single generic user-facing error so we
    never leak whether an email exists or why exactly auth failed.
    """


@runtime_checkable
class IdentityProvider(Protocol):
    key: str
    """Registry key used in URLs + the `oauth_identities.provider` column."""
    kind: str
    """Informational: "oauth2" | "oidc" | "saml" | "mock"."""

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str | None) -> str:
        """Build the provider redirect URL.

        OIDC adapters embed `nonce`; pure-OAuth2 adapters ignore it. A SAML
        adapter would return its SP-initiated binding URL carrying `state` as
        RelayState.
        """
        ...

    async def fetch_profile(
        self, *, code: str, redirect_uri: str, nonce: str | None
    ) -> FederatedProfile:
        """Exchange the returned `code` for a normalized profile.

        Raises `OAuthError` on any exchange/verification failure.
        """
        ...
