"""TOTP multi-factor auth: enrollment, activation, verification, recovery codes.

The base32 secret is stored encrypted at rest (``secret_box``); the secret is
written PENDING at enrollment and only activated (``mfa_enabled``) once the user
proves possession with a valid code. Backup codes are single-use recovery codes,
stored only as HMAC hashes (``token_hash``) and shown to the user exactly once.
"""

from __future__ import annotations

import json
import secrets

from alkera_core.auth import totp
from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret
from alkera_core.auth.token_hash import hash_lookup_token, lookup_token_digests
from alkera_core.brand import product_name
from alkera_core.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import audit as audit_services

_BACKUP_CODE_COUNT = 10


class MfaError(Exception):
    """MFA operation refused (already enabled, not enrolled, invalid code)."""


def _new_backup_code() -> str:
    # 10 hex chars, dash-grouped for readability: "a1b2c-3d4e5".
    raw = secrets.token_hex(5)
    return f"{raw[:5]}-{raw[5:]}"


def begin_enrollment(user: User) -> tuple[str, str]:
    """Generate + persist a PENDING secret; return (secret, otpauth URI)."""
    if user.mfa_enabled:
        raise MfaError("MFA is already enabled")
    secret = totp.generate_secret()
    user.mfa_secret_encrypted = encrypt_secret(secret)
    uri = totp.provisioning_uri(secret, account=user.email, issuer=product_name())
    return secret, uri


def confirm_enrollment(user: User, code: str) -> list[str]:
    """Activate MFA after the user proves a valid code; return the backup codes."""
    if user.mfa_enabled:
        raise MfaError("MFA is already enabled")
    if not user.mfa_secret_encrypted:
        raise MfaError("Start enrollment first")
    step = totp.matching_counter(decrypt_secret(user.mfa_secret_encrypted), code)
    if step is None:
        raise MfaError("Invalid code")
    # Spend the enrolling code too: without this the same digits still satisfy the
    # next step-up, so the code the user just typed into the setup form would
    # remain live for the rest of its window.
    user.mfa_last_used_counter = step
    codes = [_new_backup_code() for _ in range(_BACKUP_CODE_COUNT)]
    user.mfa_backup_codes = json.dumps([hash_lookup_token(c) for c in codes])
    user.mfa_enabled = True
    return codes


def spend_code_unlocked(user: User, code: str) -> bool:
    """True if `code` is a valid TOTP OR an unused backup code. Both are CONSUMED
    (single-use) — the caller must commit.

    UNLOCKED: this is the decision on its own, and it is a read-modify-write, so
    two callers running it concurrently against the same account both read the
    pre-spend state and both accept the same code. Every request path must go
    through :func:`verify_code_locked` (or hold :func:`lock_account` already);
    this name exists so a caller cannot reach the unserialized form by accident.

    A TOTP is spent by recording the time step it matched: skew tolerance accepts
    a code across several steps, so without that record the same digits are
    replayable for the width of the window by anyone who observes them once.
    """
    code = (code or "").strip()
    if user.mfa_secret_encrypted:
        step = totp.matching_counter(
            decrypt_secret(user.mfa_secret_encrypted),
            code,
            last_used_counter=user.mfa_last_used_counter,
        )
        if step is not None:
            user.mfa_last_used_counter = step
            return True
    # Backup codes are stored hashed; match the active pepper's digest OR any
    # retired one (so a pepper rotation doesn't void already-issued codes), and
    # consume the exact stored digest that matched.
    stored: list[str] = json.loads(user.mfa_backup_codes or "[]")
    for candidate in lookup_token_digests(code):
        if candidate in stored:
            stored.remove(candidate)
            user.mfa_backup_codes = json.dumps(stored)
            return True
    return False


async def lock_account(db: AsyncSession, user: User) -> None:
    """Serialize this account's factor state for the rest of the transaction.

    Spending a factor is a read-modify-write: read the last accepted step (or the
    unused backup digests), decide, write what remains. Two requests that overlap
    would both read the pre-spend state and both accept the SAME code -- which is
    the shape of a relay phishing kit racing the real login, the exact case
    single-use exists to stop, so leaving it unserialized would give back most of
    what recording the step buys.

    Taking the row lock first makes the second request wait and then read the
    spent state, so it refuses. The lock is released by the caller's commit,
    which every one of these paths reaches within the same request.

    A row lock, not an advisory lock: the invariant is per-account state on a row
    that always exists, so the row IS the natural thing to serialize on. An
    advisory lock is what the CI-token ceiling needs, because a COUNT across rows
    that do not exist yet has nothing to lock -- it buys a shared key space this
    does not need, and one another subsystem could collide with.
    """
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    # Every column the factor decision reads, not just the ones it writes. A
    # caller that kept a stale `mfa_enabled` would misread a state error ("already
    # enabled", "not enabled") as a rejected code and charge the account's lockout
    # budget for someone else's concurrent success.
    await db.refresh(
        user,
        attribute_names=[
            "mfa_enabled",
            "mfa_secret_encrypted",
            "mfa_backup_codes",
            "mfa_last_used_counter",
        ],
    )


async def verify_code_locked(db: AsyncSession, user: User, code: str) -> bool:
    """Spend a factor, serialized on the account row. THE path for a request.
    A backup code spent is put on the person's security log, in the same
    transaction as the spend."""
    await lock_account(db, user)
    unused = backup_codes_remaining(user)
    spent = spend_code_unlocked(user, code)
    if spent and backup_codes_remaining(user) < unused:
        await audit_services.record_security_event(
            db,
            user_id=user.id,
            event="auth.mfa_backup_code_used",
            detail={"remaining": backup_codes_remaining(user)},
        )
    return spent


def disable(user: User, code: str) -> None:
    """Turn MFA off — requires a valid code so a hijacked session can't do it freely."""
    if not user.mfa_enabled:
        raise MfaError("MFA is not enabled")
    if not spend_code_unlocked(user, code):
        raise MfaError("Invalid code")
    user.mfa_secret_encrypted = None
    user.mfa_enabled = False
    user.mfa_backup_codes = None
    # A later enrollment mints a new secret, so a step spent against the old one
    # says nothing about the new one and would only refuse legitimate codes.
    user.mfa_last_used_counter = None


def backup_codes_remaining(user: User) -> int:
    if not user.mfa_backup_codes:
        return 0
    return len(json.loads(user.mfa_backup_codes))
