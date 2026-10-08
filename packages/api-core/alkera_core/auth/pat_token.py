"""Personal-access-token minting — a user's long-lived bearer credential.

The same shape as the CI token: a greppable prefix so a leaked secret can be
recognised in a log or a repository, 384 bits of randomness after it, and only
the HMAC-SHA256 digest under the shared lookup-token pepper persisted. The raw
secret exists in the mint return value and nowhere else.
"""

from __future__ import annotations

import secrets

from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.token_prefixes import PAT_TOKEN_PREFIX


def mint_pat_token() -> tuple[str, str]:
    """Return ``(raw_secret, token_hash)``. Persist only the hash; show raw once."""
    raw = PAT_TOKEN_PREFIX + secrets.token_urlsafe(48)
    return raw, hash_pat_token(raw)


def hash_pat_token(raw: str) -> str:
    """The digest a presented token is stored under (the active pepper). Read
    paths match against every pepper's digest via ``lookup_token_digests``."""
    return hash_lookup_token(raw)


def looks_like_pat_token(raw: str) -> bool:
    return raw.startswith(PAT_TOKEN_PREFIX)


__all__ = ["PAT_TOKEN_PREFIX", "hash_pat_token", "looks_like_pat_token", "mint_pat_token"]
