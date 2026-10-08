"""Google sign-in — OIDC via the standard Google issuer.

Google returns `given_name` / `family_name` in the id_token, so first/last map
cleanly. `email_verified` is honored (Google verifies Workspace + most consumer
addresses). `prompt=select_account` lets users pick when they have several.
"""

from __future__ import annotations

from backend.auth.oauth.oidc import OidcProvider

_GOOGLE_ISSUER = "https://accounts.google.com"


def google_provider(*, client_id: str, client_secret: str) -> OidcProvider:
    return OidcProvider(
        key="google",
        issuer=_GOOGLE_ISSUER,
        client_id=client_id,
        client_secret=client_secret,
        scopes=("openid", "email", "profile"),
        extra_auth_params={"prompt": "select_account"},
        # Google has historically issued both forms of the iss claim.
        accepted_issuers=(_GOOGLE_ISSUER, "accounts.google.com"),
    )
