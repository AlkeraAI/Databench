"""Federated-login provider layer.

A provider-agnostic interface (`IdentityProvider`) + a normalized result
(`FederatedProfile`) + a registry. Google and GitHub are the first adapters;
enterprise OIDC SSO and SAML are future adapters that implement the same
interface and write the same `OAuthIdentity` rows — no changes to the routes or
the decision tree. See `apps/cli/alkera_cli/harness/README.md` style: read this
package's flow in `backend/services/identity/oauth.py`.
"""

from __future__ import annotations

from backend.auth.oauth.base import IdentityProvider
from backend.auth.oauth.profile import FederatedProfile
from backend.auth.oauth.registry import (
    ProviderRegistry,
    UnknownProviderError,
    build_registry,
    get_registry,
)

__all__ = [
    "FederatedProfile",
    "IdentityProvider",
    "ProviderRegistry",
    "UnknownProviderError",
    "build_registry",
    "get_registry",
]
