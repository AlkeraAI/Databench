"""Email verification token issue + consume.

A pending verification token lives directly on the user row (single token
at a time — resending overwrites). Verifying clears the token and stamps
`email_verified_at`.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from alkera_core.auth import hash_lookup_token, lookup_token_digests
from alkera_core.bans import banned_predicate
from alkera_core.config import settings
from alkera_core.events import (
    Entity,
    EventType,
    actor_for_user,
    actor_system,
    emit,
    user_visibility,
)
from alkera_core.models import OrgMembership, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.infra import now as _now

DEFAULT_TTL = timedelta(days=2)

#: The actor a verification is recorded under when nobody in particular did it
#: (the dev seed, the bootstrap admin).
SYSTEM_ACTOR = "backend:email_verification_service"


def resend_available_at(user: User) -> datetime | None:
    """The instant the next self-service verification resend is allowed, or
    ``None`` when none applies (already verified, or no pending token — a
    resend would be allowed immediately). May be in the past; callers compare
    against now. Derived from the pending token's expiry (``expires_at - TTL``
    is the issue instant), so no extra column is needed.

    Single source of truth for the resend route's 429 and the
    ``verification_resend_available_at`` field clients poll on /auth/me.
    """
    if user.email_verified_at is not None or user.email_verification_expires_at is None:
        return None
    issued_at = user.email_verification_expires_at - DEFAULT_TTL
    return issued_at + timedelta(seconds=settings.email_verification_resend_cooldown_seconds)


class VerificationError(Exception):
    """Domain-level verification failure (already verified, expired, unknown token)."""


#: The verification sender: ``(user, *, token) -> delivered``.
VerificationSender = Callable[..., Awaitable[bool]]


def _new_token() -> str:
    return secrets.token_urlsafe(48)


async def get_by_token(db: AsyncSession, token: str) -> User | None:
    # Only the keyed hash is stored; match the active OR any retired pepper's
    # digest so a pepper rotation doesn't void an outstanding verification link.
    # A banned holder's link answers as no link at all.
    row = (
        await db.execute(
            select(User, banned_predicate()).where(
                User.email_verification_token.in_(lookup_token_digests(token))
            )
        )
    ).first()
    if row is None or row[1]:
        return None
    user: User = row[0]
    return user


async def issue_token(db: AsyncSession, user: User) -> str:
    """Generate a new pending verification token. Overwrites any existing
    token. Idempotent for already-verified users — they get a fresh token
    too (covers email-change-reverification later, but that flow doesn't
    exist yet).

    Returns the RAW token (it goes into the emailed link); only its keyed
    HMAC hash is persisted, so a DB read can't replay the link.
    """
    raw_token = _new_token()
    user.email_verification_token = hash_lookup_token(raw_token)
    user.email_verification_expires_at = _now() + DEFAULT_TTL
    await db.flush()
    return raw_token


async def issue_and_send(db: AsyncSession, user: User, *, send: VerificationSender) -> bool:
    """Mint a fresh token and mail it to ``user.email`` — the one path every
    "we sent you a link" answer goes through (the resend route, an email change).
    Returns whether the relay took the mail.

    Issuing overwrites any outstanding token, so a link mailed to an earlier
    address stops verifying. A refused send leaves NO pending token: the pending
    token is what arms the resend cooldown and what clients read (via
    ``resend_available_at``) as "a link is in the inbox", so neither may outlive
    a send that did not happen.
    """
    token = await issue_token(db, user)
    if await send(user, token=token):
        return True
    user.email_verification_token = None
    user.email_verification_expires_at = None
    await db.flush()
    return False


async def mark_verified(db: AsyncSession, user: User, *, by_user: bool = False) -> User:
    """Used by the dev seed and the verify-token route after successful
    consumption. Announces the verification to that one user's own event
    stream (the banner they are looking at is the only thing it changes).

    ``by_user`` records the person as the actor (they proved the address);
    otherwise the system is. Each org's row names that org as the person's,
    so no org's stream carries the id of another org the person belongs to."""
    user.email_verified_at = _now()
    # The token digest STAYS. It is spent — the expiry is what makes it
    # unspendable, and `consume_token` refuses an already-verified user before
    # it looks at the expiry anyway. Keeping the digest is what lets a link
    # visited after the address is already verified still resolve to its owner
    # and answer "already verified" instead of the indistinguishable "token not
    # found". Both happen in normal use: an invited member is verified the
    # moment they sign up, and the SPA double-fires the verify request.
    user.email_verification_expires_at = None
    await db.flush()
    # The address is the identity's: the person may be looking at any of their
    # orgs, so each org's stream carries it, to them alone.
    orgs = (
        await db.execute(select(OrgMembership.org_team_id).where(OrgMembership.user_id == user.id))
    ).scalars()
    for org_id in orgs.all():
        await emit(
            db,
            org_id=org_id,
            type=EventType.USER_EMAIL_VERIFIED,
            entity=Entity.USER,
            entity_id=str(user.id),
            visibility=user_visibility(user.id),
            actor=actor_for_user(user, org_id=org_id) if by_user else actor_system(SYSTEM_ACTOR),
        )
    return user


async def consume_token(db: AsyncSession, token: str) -> User:
    """Verify the address a token was mailed to. The holder of the token is
    the one acting, so the announcement is recorded under that user."""
    user = await get_by_token(db, token)
    if user is None:
        raise VerificationError("Verification token not found")
    if user.email_verified_at is not None:
        # Already verified. The digest is left in place so every later visit to
        # the same link gets this same answer rather than degrading to "token
        # not found" — the SPA renders it as success.
        raise VerificationError("Email is already verified")
    if user.email_verification_expires_at is None or user.email_verification_expires_at < _now():
        raise VerificationError("Verification token has expired")
    return await mark_verified(db, user, by_user=True)


#: The name the ``backend.services.identity`` package exports for this.
verification_resend_available_at = resend_available_at
