"""Keyed-hash primitive for single-use lookup tokens (alkera_core.auth.token_hash)."""

from __future__ import annotations

import hashlib
import hmac

from alkera_core.auth import hash_lookup_token
from alkera_core.config import settings


def test_hash_is_deterministic():
    assert hash_lookup_token("a-token-value") == hash_lookup_token("a-token-value")


def test_distinct_inputs_yield_distinct_hashes():
    assert hash_lookup_token("token-a") != hash_lookup_token("token-b")


def test_hash_is_sha256_hex_width():
    digest = hash_lookup_token("anything")
    assert len(digest) == 64
    int(digest, 16)  # must be valid hex


def test_hash_is_hmac_keyed_by_the_pepper_not_bare_sha256():
    # The whole point: it's an HMAC-SHA256 under the server-side pepper, so a
    # DB-only leak can't offline-verify a guessed token without also stealing the
    # pepper. Pin both that it matches the keyed HMAC and that it is NOT the
    # unkeyed digest (which a leak + public algorithm could reproduce).
    raw = "some-raw-token"
    expected = hmac.new(
        settings.effective_token_hash_pepper.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert hash_lookup_token(raw) == expected
    assert hash_lookup_token(raw) != hashlib.sha256(raw.encode("utf-8")).hexdigest()
