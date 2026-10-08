"""RFC 6238 TOTP (+ RFC 4226 HOTP) — in-house, stdlib only.

Implemented here (not a dependency) because the algorithm is small, stable, and
exactly specified, and a pure-stdlib version avoids a runtime dep that the offline
build + nuitka packaging would have to carry. Correctness is pinned against the
RFC 6238 test vectors in the tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

_PERIOD = 30
_DIGITS = 6


def generate_secret() -> str:
    """A fresh base32 TOTP secret (160 bits, the RFC-recommended size)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**_DIGITS)).zfill(_DIGITS)


def matching_counter(
    secret_b32: str,
    code: str,
    *,
    at: float | None = None,
    window: int = 1,
    last_used_counter: int | None = None,
) -> int | None:
    """The time step `code` is a valid TOTP for, or None if it is valid for none.

    `window` is the clock-skew tolerance in 30-second steps either side of now,
    so a code is accepted across `2 * window + 1` steps. That band is also a
    replay window: RFC 6238 §5.2 requires a validated code to be refused when it
    is presented a second time, and skew tolerance alone cannot do that — the
    verifier has to remember what it already accepted.

    `last_used_counter` is that memory. Pass the step of the caller's most
    recently accepted code and every step at or before it is refused, which makes
    an accepted code single-use for as long as the caller persists the step this
    returns. Callers that keep no such record pass None and keep the plain
    skew-tolerant behavior.

    Constant-time compare; rejects malformed input.
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != _DIGITS:
        return None
    counter = int((time.time() if at is None else at) // _PERIOD)
    for drift in range(-window, window + 1):
        step = counter + drift
        if step < 0:  # no negative counters near the unix epoch
            continue
        if last_used_counter is not None and step <= last_used_counter:
            continue
        if hmac.compare_digest(_hotp(secret_b32, step), code):
            return step
    return None


def verify(
    secret_b32: str,
    code: str,
    *,
    at: float | None = None,
    window: int = 1,
    last_used_counter: int | None = None,
) -> bool:
    """True if `code` is a valid TOTP for `secret_b32` now (±`window` periods of
    clock-skew tolerance, and strictly after `last_used_counter` when given).

    Use `matching_counter` instead when the accepted step is to be recorded, since
    that record is what stops the same code being spent twice inside the window.
    """
    return (
        matching_counter(
            secret_b32, code, at=at, window=window, last_used_counter=last_used_counter
        )
        is not None
    )


def provisioning_uri(secret_b32: str, *, account: str, issuer: str) -> str:
    """An `otpauth://` URI for an authenticator app (Google Authenticator, 1Password…)."""
    label = quote(f"{issuer}:{account}", safe=":")
    params = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": _DIGITS,
            "period": _PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{params}"
