"""Provider registry — maps a provider key to its adapter, from settings.

Only configured providers are registered, so `GET /oauth/providers` and the
sign-in buttons reflect what's actually wired up. A future per-org OIDC/SAML
provider would resolve by key here too (constructed from an org config row)
without changing callers.
"""

from __future__ import annotations

from alkera_core.config import Settings
from alkera_core.config import settings as default_settings

from backend.auth.oauth.base import IdentityProvider
from backend.auth.oauth.github import GitHubProvider
from backend.auth.oauth.google import google_provider
from backend.auth.oauth.mock import MockProvider

# Providers a regular org can toggle via OrgSettings (`allow_login_<key>`).
# The mock provider is dev/test-only and is never org-gated.
ORG_GATED_PROVIDERS = ("google", "github")


class UnknownProviderError(Exception):
    """Requested provider is not configured/registered."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, IdentityProvider] = {}

    def register(self, provider: IdentityProvider) -> None:
        self._providers[provider.key] = provider

    def get(self, key: str) -> IdentityProvider:
        try:
            return self._providers[key]
        except KeyError as exc:
            raise UnknownProviderError(key) from exc

    def has(self, key: str) -> bool:
        return key in self._providers

    def keys(self) -> list[str]:
        return list(self._providers)


def build_registry(settings: Settings = default_settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    if settings.google_oauth_configured:
        assert settings.oauth_google_client_id is not None
        assert settings.oauth_google_client_secret is not None
        registry.register(
            google_provider(
                client_id=settings.oauth_google_client_id,
                client_secret=settings.oauth_google_client_secret,
            )
        )
    if settings.github_oauth_configured:
        assert settings.oauth_github_client_id is not None
        assert settings.oauth_github_client_secret is not None
        registry.register(
            GitHubProvider(
                client_id=settings.oauth_github_client_id,
                client_secret=settings.oauth_github_client_secret,
            )
        )
    # Defense in depth alongside the `_validate_oauth_mock` config validator:
    # the mock provider is a credential-less bypass and is registered ONLY in
    # local dev, never in staging/production even if the flag were forced on.
    if settings.is_local and settings.oauth_mock_enabled:
        registry.register(MockProvider())
    return registry


_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """Process-wide singleton (built lazily from settings)."""
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry
