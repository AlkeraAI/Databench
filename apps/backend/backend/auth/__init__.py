"""Auth layer: password hashing, JWT tokens, FastAPI permission dependencies."""

from __future__ import annotations

from alkera_core.auth import (
    InvalidTokenError,
    SessionClaims,
    decode_session_token,
)

from backend.auth.dependencies import (
    current_user,
    require_org_admin,
    require_platform_admin,
    require_platform_staff,
    require_team_admin,
)
from backend.auth.password import hash_password, verify_password

__all__ = [
    "InvalidTokenError",
    "SessionClaims",
    "current_user",
    "decode_session_token",
    "hash_password",
    "require_org_admin",
    "require_platform_admin",
    "require_platform_staff",
    "require_team_admin",
    "verify_password",
]
