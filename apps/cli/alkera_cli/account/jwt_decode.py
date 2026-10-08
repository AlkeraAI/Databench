"""Minimal JWT payload decoder.

Lightweight standalone module so both the CLI (`alkera login`) and the
daemon's auth methods can read `exp` without dragging the rest of
``alkera_cli.main`` into the import graph.

We never verify signatures here — the backend issued the token, the
``expires_at`` value is purely for UI labelling.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any


def jwt_claims(token: str) -> dict[str, Any] | None:
    """The JWT payload, unverified, or None when the token is not a JWT.

    For labels and for keying a stored sign-in by the identity the server put
    in the token; never for a trust decision (the server verifies every
    request)."""
    try:
        _header, payload_b64, _sig = token.split(".")
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def jwt_expires_at(token: str) -> datetime:
    """Decode the JWT payload (no signature verification) to read `exp`.

    Falls back to "90 days from now" if the token is malformed — best
    effort, since the value is only used to render a "session expires
    on…" label.
    """
    try:
        _header, payload_b64, _sig = token.split(".")
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return datetime.now(UTC) + timedelta(days=90)


__all__ = ["jwt_claims", "jwt_expires_at"]
