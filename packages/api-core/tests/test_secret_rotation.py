"""Graceful rotation of the two server-side auth secrets.

- ``AUTH_JWT_SECRET`` → ``AUTH_JWT_SECRET_PREVIOUS``: a rotation must keep
  already-issued session / CLI tokens verifiable until they expire instead of
  logging every session out at once. Encoding always uses the active secret.
- ``TOKEN_HASH_PEPPER`` → ``TOKEN_HASH_PEPPER_PREVIOUS``: a rotation must keep
  outstanding lookup tokens (reset / verify / invite / proxy) resolvable. New
  digests are written under the active pepper.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from alkera_core.auth import InvalidTokenError, decode_session_token, encode_session_token
from alkera_core.auth.token_hash import _digest, hash_lookup_token, lookup_token_digests
from alkera_core.config import settings


def _session_token(secret: str, *, ttl: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": uuid4().hex,
            "email": "u@acme.example",
            "org_team_id": uuid4().hex,
            "platform_role": None,
            "jti": uuid4().hex,
            "iat": now,
            "exp": now + ttl,
        },
        secret,
        algorithm=settings.auth_jwt_alg,
    )


# --------------------------------------------------------------------------- #
# JWT secret rotation
# --------------------------------------------------------------------------- #


def test_token_signed_by_a_previous_secret_still_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "secret-old")
    token = _session_token("secret-old")  # issued before the rotation
    claims = decode_session_token(token)  # still accepted under the retired secret
    assert claims.email == "u@acme.example"


def test_token_signed_by_an_unknown_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "secret-old")
    with pytest.raises(InvalidTokenError):
        decode_session_token(_session_token("secret-stranger"))


def test_encode_always_uses_the_active_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "secret-old")
    token, _claims = encode_session_token(
        user_id=uuid4(), email="u@acme.example", org_team_id=uuid4(), platform_role=None
    )
    # Verifies under the active secret...
    jwt.decode(token, "secret-new", algorithms=[settings.auth_jwt_alg])
    # ...and NOT under the retired one (encode never reaches for a previous secret).
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "secret-old", algorithms=[settings.auth_jwt_alg])


def test_dropping_the_previous_secret_closes_the_door(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "")  # retired value removed
    with pytest.raises(InvalidTokenError):
        decode_session_token(_session_token("secret-old"))


def test_an_expired_token_is_rejected_even_under_the_active_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "secret-old")
    with pytest.raises(InvalidTokenError):
        decode_session_token(_session_token("secret-new", ttl=-10))


def test_multiple_previous_secrets_are_each_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_jwt_secret", "secret-new")
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "secret-old1, secret-old2")
    assert decode_session_token(_session_token("secret-old2")).email == "u@acme.example"


# --------------------------------------------------------------------------- #
# Token-hash pepper rotation
# --------------------------------------------------------------------------- #


def test_lookup_digests_is_just_the_active_digest_without_a_previous() -> None:
    raw = "tok-" + uuid4().hex
    assert lookup_token_digests(raw) == [hash_lookup_token(raw)]


def test_lookup_digests_finds_a_token_hashed_under_a_previous_pepper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "token_hash_pepper", "pepper-new")
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "pepper-old")
    raw = "tok-" + uuid4().hex
    stored_under_old = _digest("pepper-old", raw)  # what's already in the DB

    candidates = lookup_token_digests(raw)
    # Active digest is FIRST (the common case stays a single indexed lookup)...
    assert candidates[0] == _digest("pepper-new", raw) == hash_lookup_token(raw)
    # ...and the old digest is a candidate, so the stored row is still found.
    assert stored_under_old in candidates
    # The active-only WRITE digest would NOT match the old row on its own.
    assert hash_lookup_token(raw) != stored_under_old


def test_lookup_digests_dedupes_a_previous_equal_to_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "token_hash_pepper", "pepper-x")
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "pepper-x, pepper-y")
    digests = lookup_token_digests("tok")
    assert len(digests) == len(set(digests)) == 2  # x collapsed, x + y remain


def test_dropping_the_previous_pepper_strands_old_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "token_hash_pepper", "pepper-new")
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "")
    raw = "tok-" + uuid4().hex
    assert _digest("pepper-old", raw) not in lookup_token_digests(raw)
