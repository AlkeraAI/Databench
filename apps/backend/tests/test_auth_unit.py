"""Unit tests for password + token primitives. No DB."""

from __future__ import annotations

import base64
import os
import time
from uuid import uuid4

import pytest
from alkera_core.auth import (
    InvalidTokenError,
    decode_session_token,
    encode_session_token,
)
from alkera_core.config import settings
from alkera_core.models import PlatformRole
from argon2 import extract_parameters
from backend.auth.password import hash_password, verify_password


def test_hash_then_verify_round_trips():
    hashed = hash_password("correct horse")
    assert verify_password("correct horse", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_verify_rejects_none_hash():
    assert verify_password("anything", None) is False


@pytest.mark.parametrize(
    ("profile", "time_cost", "memory_cost", "parallelism"),
    [
        pytest.param("production", 3, 65536, 4, id="production"),
        pytest.param("fast", 1, 8, 1, id="fast"),
    ],
)
def test_the_profile_decides_the_cost_written_into_the_hash(
    profile: str,
    time_cost: int,
    memory_cost: int,
    parallelism: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """Both profiles are spelled out, the production one included: it is today's
    argon2-cffi default written down rather than inherited, so a release that
    re-tunes that default has to come through this test instead of silently
    changing what a login costs."""
    monkeypatch.setattr(settings, "password_hash_profile", profile)
    params = extract_parameters(hash_password("correct horse"))
    assert (params.time_cost, params.memory_cost, params.parallelism) == (
        time_cost,
        memory_cost,
        parallelism,
    )


@pytest.mark.parametrize(
    ("minted_under", "read_under", "minted_memory_cost"),
    [
        pytest.param("production", "fast", 65536, id="a-real-row-read-by-a-fast-process"),
        pytest.param("fast", "production", 8, id="a-fixture-row-read-by-a-real-process"),
    ],
)
def test_a_hash_outlives_the_profile_it_was_minted_under(
    minted_under: str,
    read_under: str,
    minted_memory_cost: int,
    monkeypatch: pytest.MonkeyPatch,
):
    """What makes the profile safe to change on a database of live passwords:
    argon2 writes its parameters into the PHC string, so verification reads the
    cost off the hash it was handed, never off the current configuration.

    The wrong password must still be refused across the switch — a verifier that
    answered "parameters differ, close enough" would satisfy the first assertion
    on its own — and the stored row must come back out at the cost it went in at,
    because nothing here re-hashes anything.
    """
    monkeypatch.setattr(settings, "password_hash_profile", minted_under)
    hashed = hash_password("correct horse")

    monkeypatch.setattr(settings, "password_hash_profile", read_under)
    assert verify_password("correct horse", hashed) is True
    assert verify_password("wrong horse", hashed) is False
    assert extract_parameters(hashed).memory_cost == minted_memory_cost


def test_the_test_session_hashes_at_the_fast_profile():
    """The session-wide pin lives in the repo-root conftest, and losing it is
    invisible: every suite stays green and quietly goes back to paying tens of
    milliseconds of argon2 per fixture, three or four times a test. A run that
    asked for another profile out loud is exempt — that is the documented way to
    measure the real cost — but a run that asked for nothing must get `fast`."""
    asked_for = os.environ.get("PASSWORD_HASH_PROFILE")
    if asked_for not in (None, "fast"):
        pytest.skip(f"this run explicitly asked for the {asked_for!r} profile")
    assert asked_for is not None, "the repo-root conftest no longer pins the hash profile"
    assert settings.password_hash_profile == "fast"


def test_token_round_trip_preserves_claims():
    user_id = uuid4()
    org_id = uuid4()
    token, issued = encode_session_token(
        user_id=user_id,
        email="user@alkera.dev",
        org_team_id=org_id,
        platform_role=PlatformRole.ALKERA_ADMIN,
    )
    claims = decode_session_token(token)
    assert claims.user_id == user_id
    assert claims.org_team_id == org_id
    assert claims.email == "user@alkera.dev"
    assert claims.platform_role is PlatformRole.ALKERA_ADMIN
    assert claims.expires_at == issued.expires_at
    # jti is minted on encode and round-trips through decode.
    assert issued.jti is not None
    assert claims.jti == issued.jti


def test_token_round_trip_with_no_platform_role():
    token, _ = encode_session_token(
        user_id=uuid4(),
        email="x@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
    )
    claims = decode_session_token(token)
    assert claims.platform_role is None


def test_expired_token_rejected():
    past = int(time.time()) - 10**6  # way in the past
    token, _ = encode_session_token(
        user_id=uuid4(),
        email="x@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
        now=past,  # exp = past + ttl, still long ago
    )
    with pytest.raises(InvalidTokenError):
        decode_session_token(token)


def test_tampered_token_rejected():
    token, _ = encode_session_token(
        user_id=uuid4(),
        email="x@alkera.dev",
        org_team_id=uuid4(),
        platform_role=None,
    )
    header, payload, signature = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    raw[0] ^= 0x01
    tampered_signature = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(InvalidTokenError):
        decode_session_token(f"{header}.{payload}.{tampered_signature}")


def test_malformed_token_rejected():
    with pytest.raises(InvalidTokenError):
        decode_session_token("not-a-jwt")
