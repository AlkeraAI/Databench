"""CI-token minting -- the gate's org/repo-scoped, userless upload credential.

A ``CiToken`` authenticates a CI job (snapshot publish, GateReport upload) to
the backend without impersonating any user -- the ProxyToken pattern applied to
the schema gate. The raw secret is shown exactly once at mint; only the
HMAC-SHA256 digest (the shared lookup-token pepper) is persisted.
"""

from __future__ import annotations

import secrets

from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.token_prefixes import CI_TOKEN_PREFIX


def mint_ci_token() -> tuple[str, str]:
    """Return ``(raw_secret, token_hash)``. Persist only the hash; show raw once."""
    raw = CI_TOKEN_PREFIX + secrets.token_urlsafe(48)
    return raw, hash_lookup_token(raw)


def looks_like_ci_token(raw: str) -> bool:
    return raw.startswith(CI_TOKEN_PREFIX)


__all__ = ["CI_TOKEN_PREFIX", "looks_like_ci_token", "mint_ci_token"]
