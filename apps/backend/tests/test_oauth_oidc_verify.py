"""OIDC id_token verification — the security core of the OIDC login path.

No network, no real credentials: we mint a local RSA keypair, publish its public
half as a JWKS, sign id_tokens, and assert `OidcProvider._verify_id_token`
accepts a correctly-signed/claimed token and REJECTS every tampered variant
(bad signature, wrong issuer/audience, expired, nonce mismatch, and the
alg-confusion attacks `none` / HS256-with-the-public-key). Runs in the default
suite — this is exactly the logic an attacker would probe.
"""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from authlib.jose import JsonWebKey
from authlib.jose import jwt as jose_jwt
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.oidc import OidcProvider

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = pytest.mark.xdist_group("oauth_oidc_verify")

ISSUER = "https://issuer.test"
CLIENT_ID = "client-aud-123"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair() -> JsonWebKey:
    return JsonWebKey.generate_key("RSA", 2048, {"kid": KID}, is_private=True)


@pytest.fixture(scope="module")
def jwks(keypair: JsonWebKey) -> dict[str, Any]:
    return {"keys": [keypair.as_dict(is_private=False)]}


def _provider() -> OidcProvider:
    return OidcProvider(key="t", issuer=ISSUER, client_id=CLIENT_ID, client_secret="x")


def _claims(**override: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": now + 600,
        "iat": now,
        "sub": "user-1",
        "email": "a@example.com",
        "email_verified": True,
        "nonce": "NONCE",
    }
    claims.update(override)
    return claims


def _sign(keypair: JsonWebKey, claims: dict[str, Any], *, kid: str = KID) -> str:
    token = jose_jwt.encode({"alg": "RS256", "kid": kid}, claims, keypair)
    return token.decode("ascii") if isinstance(token, bytes) else token


def test_valid_token_verifies(keypair: JsonWebKey, jwks: dict[str, Any]) -> None:
    claims = _provider()._verify_id_token(_sign(keypair, _claims()), jwks=jwks, nonce="NONCE")
    assert claims["sub"] == "user-1"
    assert claims["email"] == "a@example.com"


def test_wrong_audience_rejected(keypair: JsonWebKey, jwks: dict[str, Any]) -> None:
    token = _sign(keypair, _claims(aud="someone-else"))
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")


def test_wrong_issuer_rejected(keypair: JsonWebKey, jwks: dict[str, Any]) -> None:
    token = _sign(keypair, _claims(iss="https://evil.test"))
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")


def test_expired_token_rejected(keypair: JsonWebKey, jwks: dict[str, Any]) -> None:
    now = int(time.time())
    token = _sign(keypair, _claims(exp=now - 3600, iat=now - 7200))
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")


def test_nonce_mismatch_rejected(keypair: JsonWebKey, jwks: dict[str, Any]) -> None:
    token = _sign(keypair, _claims(nonce="NONCE"))
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="DIFFERENT")


def test_foreign_key_signature_rejected(jwks: dict[str, Any]) -> None:
    # Signed by a DIFFERENT key (same kid) not matching the published JWKS.
    attacker = JsonWebKey.generate_key("RSA", 2048, {"kid": KID}, is_private=True)
    token = _sign(attacker, _claims())
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")


def test_alg_none_rejected(jwks: dict[str, Any]) -> None:
    # Unsigned `alg: none` token — the classic OIDC bypass.
    token = pyjwt.encode(_claims(), key=None, algorithm="none", headers={"kid": KID})  # type: ignore[arg-type]
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")


def test_hs256_confusion_rejected(jwks: dict[str, Any]) -> None:
    # alg-confusion: the verifier locks alg to RS256/ES256, so a symmetric
    # HS256 token (the public-key-as-HMAC-secret attack family) is rejected
    # outright regardless of the secret used.
    secret = "attacker-controlled-hmac-secret-key-32b"  # ≥32 bytes (avoids PyJWT warning)
    token = pyjwt.encode(_claims(), key=secret, algorithm="HS256", headers={"kid": KID})
    with pytest.raises(OAuthError):
        _provider()._verify_id_token(token, jwks=jwks, nonce="NONCE")
