"""TOTP correctness pinned against the RFC 6238 Appendix-B test vectors (SHA1),
truncated to our 6 digits, plus self-consistency + rejection cases."""

from __future__ import annotations

import pytest
from alkera_core.auth import totp

# RFC 6238 §B uses the ASCII secret "12345678901234567890" → this base32.
_RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    ("at", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_rfc6238_vectors(at: int, code: str) -> None:
    assert totp.verify(_RFC_SECRET, code, at=at, window=0)
    assert not totp.verify(_RFC_SECRET, "000000", at=at, window=0)


def test_window_tolerates_one_period_of_skew() -> None:
    # "287082" is the RFC code for period 1 (t=59). With ±1 window it verifies for
    # an adjacent period but not for a distant one.
    assert totp.verify(_RFC_SECRET, "287082", at=59, window=1)
    assert totp.verify(_RFC_SECRET, "287082", at=89, window=1)  # period 2, ±1 ok
    assert not totp.verify(_RFC_SECRET, "287082", at=300, window=1)  # period 10, far
    assert totp.generate_secret()  # a fresh secret is non-empty base32


def test_rejects_malformed_codes() -> None:
    assert not totp.verify(_RFC_SECRET, "12345", at=59)  # too short
    assert not totp.verify(_RFC_SECRET, "abcdef", at=59)  # non-digit
    assert not totp.verify(_RFC_SECRET, "", at=59)


def test_provisioning_uri_shape() -> None:
    uri = totp.provisioning_uri(_RFC_SECRET, account="ada@acme.test", issuer="Acme Data")
    assert uri.startswith("otpauth://totp/Acme%20Data:ada%40acme.test?")
    assert f"secret={_RFC_SECRET}" in uri
    assert "issuer=Acme+Data" in uri
