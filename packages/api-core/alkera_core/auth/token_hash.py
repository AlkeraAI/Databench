"""Keyed hashing of single-use lookup tokens.

Password-reset, email-verification, and invitation tokens are high-entropy
secrets the server looks up by value. Storing them verbatim means a DB read
(SQLi, leaked backup, replica) yields directly-replayable links. We instead
store a deterministic HMAC-SHA256 of the token (keyed by ``TOKEN_HASH_PEPPER``)
and look up by that digest:

- Deterministic, so lookup stays an O(1) indexed equality. (bcrypt/argon2 would
  use a per-row salt, forcing an O(n) scan + a slow verify per row to find a
  match — wrong tool for a lookup token, and a DoS vector.)
- Keyed by a server-side pepper, so a DB-only leak can't even offline-confirm a
  guessed token without also stealing the pepper (which lives in Secrets
  Manager, not the database).
- The raw token still travels in the emailed link; only the digest is persisted.

The tokens themselves are 384-bit random (``secrets.token_urlsafe(48)``), so
they are not brute-forceable like human passwords — a fast *keyed* hash is the
correct, standard primitive here.
"""

from __future__ import annotations

import hashlib
import hmac

from alkera_core.config import settings


def _digest(pepper: str, raw_token: str) -> str:
    return hmac.new(pepper.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_lookup_token(raw_token: str) -> str:
    """Return the hex HMAC-SHA256 digest of ``raw_token`` under the ACTIVE pepper.
    Deterministic — the same token always maps to the same digest, so callers
    persist this and look up rows by it. 64 hex chars (fits ``String(96)``).

    Use this on the WRITE path (minting/storing a token). On the READ path use
    :func:`lookup_token_digests` so a pepper rotation doesn't strand outstanding
    tokens."""
    return _digest(settings.effective_token_hash_pepper, raw_token)


def lookup_token_digests(raw_token: str) -> list[str]:
    """Every digest a presented ``raw_token`` could be stored as — the ACTIVE
    pepper first, then each ``TOKEN_HASH_PEPPER_PREVIOUS``. READ paths match a
    stored digest against ANY of these (``col.in_(lookup_token_digests(raw))``),
    so rotating the pepper keeps already-issued tokens valid until they expire;
    WRITE paths keep using :func:`hash_lookup_token` (active only). The active
    digest is always first, so the common no-rotation case is a single value."""
    peppers = [settings.effective_token_hash_pepper, *settings.token_hash_pepper_previous_list]
    # De-dupe while preserving order (a previous pepper accidentally equal to the
    # active one must not produce a duplicate digest in the IN-list).
    seen: set[str] = set()
    digests: list[str] = []
    for p in peppers:
        d = _digest(p, raw_token)
        if d not in seen:
            seen.add(d)
            digests.append(d)
    return digests
