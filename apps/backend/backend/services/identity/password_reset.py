"""Password reset token issue + consume.

Mirrors `email_verification_service`: a single pending token per user
lives on the user row. Re-requesting overwrites; consumption clears.

Tokens are short-lived (1 hour) — shorter than verification because
they're a higher-impact credential.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import timedelta

from alkera_core.auth import hash_lookup_token, lookup_token_digests, revoke_all_for_user
from alkera_core.bans import banned_predicate
from alkera_core.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.password import hash_password
from backend.services.identity import lockout as lockout_service
from backend.services.infra import now as _now

DEFAULT_TTL = timedelta(hours=1)


class PasswordResetError(Exception):
    """Domain-level reset failure (unknown / expired / already-consumed token)."""


def _new_token() -> str:
    return secrets.token_urlsafe(48)


async def get_by_token(db: AsyncSession, token: str) -> User | None:
    # Only the keyed hash is stored, so look up by the hash of the presented
    # token (constant-time equality is unnecessary — 384-bit random tokens).
    # Match the active pepper's digest OR any retired one so a pepper rotation
    # doesn't invalidate an outstanding reset link.
    # A banned holder's link answers as no link at all.
    row = (
        await db.execute(
            select(User, banned_predicate()).where(
                User.password_reset_token.in_(lookup_token_digests(token))
            )
        )
    ).first()
    if row is None or row[1]:
        return None
    user: User = row[0]
    return user


async def issue_token(db: AsyncSession, user: User) -> str:
    """Generate a new pending reset token. Overwrites any prior token —
    the previous link is invalidated as soon as a new one is requested.

    Returns the RAW token (it goes into the emailed link); only its keyed
    HMAC hash is persisted, so a DB read can't replay the link.
    """
    raw_token = _new_token()
    user.password_reset_token = hash_lookup_token(raw_token)
    user.password_reset_expires_at = _now() + DEFAULT_TTL
    await db.flush()
    return raw_token


async def consume_token(
    db: AsyncSession,
    token: str,
    *,
    new_password: str,
    validate: Callable[[User], None] | None = None,
) -> User:
    """Validate the token, set the new password, and clear the token.

    ``validate`` runs once the token has resolved to an account but BEFORE
    anything is written — that ordering is what lets the route enforce a password
    policy against the real identity ("this is your own email address") without
    the check itself becoming an oracle: an invalid or expired token is refused
    first, so a caller learns nothing about an address it does not already hold a
    live token for. A raising ``validate`` leaves the token spendable, so the
    user can simply try a different password.
    """
    user = await get_by_token(db, token)
    if user is None:
        raise PasswordResetError("Password reset token not found")
    if user.password_reset_expires_at is None or user.password_reset_expires_at < _now():
        # Clear stale token bookkeeping so it's not lying around.
        user.password_reset_token = None
        user.password_reset_expires_at = None
        await db.flush()
        raise PasswordResetError("Password reset token has expired")

    if validate is not None:
        validate(user)

    user.password_hash = hash_password(new_password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    # A completed reset is proof of the address at least as strong as a login,
    # so it clears the failed-login lockout the same way a success does. Without
    # this a user who locked themselves out and then reset stays 429'd for the
    # whole cool-off, though the success page tells them they can sign in.
    lockout_service.reset(user)
    await db.flush()
    # Changing the password is a security event: kill every existing session
    # (bumps token_epoch + revokes the registry rows).
    await revoke_all_for_user(db, user.id)
    return user
