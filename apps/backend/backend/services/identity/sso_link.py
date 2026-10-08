"""Linking an org's single sign-on to an identity that already exists.

When an org's IdP asserts an email whose identity holds no active membership
in that org, the IdP is never allowed to attach the identity by itself: the
assertion is parked here (:func:`park`) for a few minutes, bound to the browser
by a cookie whose value only that browser holds, and the person confirms the
link while signed in with one of the identity's OWN methods (:func:`confirm`).
Nothing is linked and nobody joins anything until they do.

Confirming links the IdP's subject to the identity. The person joins the org
only when the org already asked them to: a pending invitation for the address,
or a membership the org's SCIM provisioned as pending. Otherwise the link is
kept, so the next invitation is accepted by a sign-in, and the person is told
to ask the org for one.

Every door here is shut while multi-org is off: the callback never parks, and
an existing request is not found.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.auth import sign_in_policy
from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.events import actor_for_user
from alkera_core.models import (
    MembershipStatus,
    OAuthIdentity,
    SsoLinkRequest,
    Team,
    User,
)
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.oauth.profile import FederatedProfile
from backend.services.org import (
    InvitationError,
    accept_invitation,
    activate_pending_membership,
    list_pending_for_email,
    membership_in,
)

#: How long a parked assertion waits for its confirmation.
LINK_TTL = timedelta(minutes=10)


class SsoLinkError(Exception):
    """A link request refused. ``status_code`` and ``code`` are what the route
    answers; ``code`` is None for the opaque 404."""

    def __init__(self, status_code: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_found() -> SsoLinkError:
    return SsoLinkError(404, None, "Not found")


@dataclass(frozen=True, slots=True)
class LinkView:
    """What the confirmation page shows: the org, and the address, masked."""

    org_team_id: UUID
    org_name: str
    email_masked: str


@dataclass(frozen=True, slots=True)
class LinkResult:
    """What a confirmation did. ``joined`` is whether the person is now an
    active member of the org."""

    org_team_id: UUID
    org_name: str
    joined: bool


def _now() -> datetime:
    return datetime.now(UTC)


def token_hash(raw: str) -> str:
    """The stored form of a cookie value: SHA-256, hex. The value itself is
    never stored, so a read of the table cannot replay a request."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mask_email(email: str) -> str:
    """``j***@acme.com``: enough for the person to recognize their address,
    not enough to read it off a shoulder-surfed screen."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


async def park(db: AsyncSession, *, org_team_id: UUID, profile: FederatedProfile) -> str:
    """Store the assertion for ``org_team_id``'s IdP and return the raw value
    the browser's cookie carries. Expired requests are deleted first, so the
    table holds no more than the last few minutes of attempts."""
    now = _now()
    await db.execute(
        delete(SsoLinkRequest)
        .where(SsoLinkRequest.expires_at <= now)
        .execution_options(synchronize_session=False)
    )
    raw = secrets.token_urlsafe(32)
    db.add(
        SsoLinkRequest(
            token_hash=token_hash(raw),
            org_team_id=org_team_id,
            provider=sign_in_policy.sso_provider_key(org_team_id),
            subject=profile.subject,
            email=(profile.email or "").lower().strip(),
            email_verified=profile.email_verified,
            raw_profile=profile.raw,
            expires_at=now + LINK_TTL,
        )
    )
    await db.flush()
    return raw


async def _live(db: AsyncSession, raw: str | None, *, lock: bool = False) -> SsoLinkRequest | None:
    """The unconsumed, unexpired request ``raw`` names, or None. Never found
    while multi-org is off."""
    if not raw or not multi_org_enabled():
        return None
    stmt = select(SsoLinkRequest).where(
        SsoLinkRequest.token_hash == token_hash(raw),
        SsoLinkRequest.consumed_at.is_(None),
        SsoLinkRequest.expires_at > _now(),
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def _assert_same_identity(row: SsoLinkRequest, user: User) -> None:
    """The request is about the address of the identity signed in. A browser
    signed in as somebody else is told so, and nothing is linked."""
    if row.email != user.email.lower().strip():
        raise SsoLinkError(
            409, "sso_link_other_account", "You're signed in to a different account."
        )


async def _org_name(db: AsyncSession, org_team_id: UUID) -> str:
    org = await db.get(Team, org_team_id)
    return org.name if org is not None else ""


async def view(db: AsyncSession, raw: str | None, *, user: User) -> LinkView:
    """The confirmation page's content for the signed-in ``user``."""
    row = await _live(db, raw)
    if row is None:
        raise _not_found()
    _assert_same_identity(row, user)
    return LinkView(
        org_team_id=row.org_team_id,
        org_name=await _org_name(db, row.org_team_id),
        email_masked=mask_email(row.email),
    )


async def _link(db: AsyncSession, row: SsoLinkRequest, *, user: User) -> None:
    """Create the identity's link to the org's IdP subject, or accept the one
    it already holds. A subject linked to another identity, or a second
    subject for the same org's IdP, is refused and nothing is written."""
    held = (
        await db.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == row.provider, OAuthIdentity.subject == row.subject
            )
        )
    ).scalar_one_or_none()
    if held is not None:
        if held.user_id != user.id:
            raise SsoLinkError(
                409,
                "sso_subject_linked",
                "This single sign-on account is linked to another account.",
            )
        return
    other = (
        await db.execute(
            select(OAuthIdentity.id).where(
                OAuthIdentity.provider == row.provider, OAuthIdentity.user_id == user.id
            )
        )
    ).first()
    if other is not None:
        raise SsoLinkError(
            409,
            "sso_provider_linked",
            "Your account is already linked to another account at this organization's "
            "single sign-on.",
        )
    try:
        async with db.begin_nested():
            db.add(
                OAuthIdentity(
                    user_id=user.id,
                    provider=row.provider,
                    subject=row.subject,
                    email_at_link=row.email,
                    email_verified=row.email_verified,
                    raw_profile=row.raw_profile,
                    last_login_at=_now(),
                )
            )
    except IntegrityError as exc:
        # A concurrent confirmation won the unique constraint.
        raise SsoLinkError(
            409,
            "sso_subject_linked",
            "This single sign-on account is linked to another account.",
        ) from exc


async def _join_by_invitation(
    db: AsyncSession, *, user: User, org_team_id: UUID, actor: dict[str, Any]
) -> bool:
    """Accept the oldest live invitation addressed to the person into the org,
    if there is one. Returns whether they joined. A refused acceptance
    (expired meanwhile, an inviter who lost the authority to invite) leaves
    nothing behind."""
    invitations = await list_pending_for_email(db, user.email, org_team_id=org_team_id)
    if not invitations:
        return False
    try:
        async with db.begin_nested():
            # The acceptance creates the membership, its seat and its team
            # chain, exactly as an invitation accepted from the inbox does.
            await accept_invitation(db, invitations[0], user=user, actor=actor)
    except InvitationError:
        return False
    return True


async def _join(db: AsyncSession, row: SsoLinkRequest, *, user: User) -> bool:
    """Activate the person's membership in the request's org, when the org
    already asked for them. Returns whether they are an active member now."""
    org_team_id = row.org_team_id
    actor = actor_for_user(user, org_id=org_team_id)
    membership = await membership_in(db, user_id=user.id, org_team_id=org_team_id)
    if membership is not None and membership.status is MembershipStatus.ACTIVE:
        return True
    if membership is not None and membership.status is MembershipStatus.PENDING:
        # The confirmation carries a sign-in through this org's IdP made a
        # moment ago; the org's policy weighs it as such.
        policy = await sign_in_policy.evaluate(
            db,
            user=user,
            org_team_id=org_team_id,
            family_id=None,
            method=sign_in_policy.SSO_METHOD,
        )
        if isinstance(policy, sign_in_policy.StepUp):
            return False
        await activate_pending_membership(db, membership, actor=actor)
        return True
    if membership is not None:
        return False
    return await _join_by_invitation(db, user=user, org_team_id=org_team_id, actor=actor)


async def confirm(db: AsyncSession, raw: str | None, *, user: User) -> LinkResult:
    """Link the parked assertion to the signed-in ``user`` and join the org
    when it asked for them. Single use: the request is consumed on success and
    a replay is not found. A refusal consumes nothing and writes nothing."""
    row = await _live(db, raw, lock=True)
    if row is None:
        raise _not_found()
    _assert_same_identity(row, user)
    membership = await membership_in(db, user_id=user.id, org_team_id=row.org_team_id)
    if membership is not None and membership.status is MembershipStatus.DEACTIVATED:
        # The org offboarded this person since the IdP asserted them.
        raise SsoLinkError(403, "account_deactivated", "This organization deactivated you.")
    await _link(db, row, user=user)
    # The request's session is held to the org the person signed in to; the
    # seat a join writes is in the org whose IdP asserted them.
    async with cross_tenant_write(db, reason="sso_link.join"):
        joined = await _join(db, row, user=user)
    row.consumed_at = _now()
    await db.flush()
    return LinkResult(
        org_team_id=row.org_team_id,
        org_name=await _org_name(db, row.org_team_id),
        joined=joined,
    )


async def cancel(db: AsyncSession, raw: str | None) -> None:
    """Consume the request without linking anything. Not found when there is
    no live request."""
    row = await _live(db, raw, lock=True)
    if row is None:
        raise _not_found()
    row.consumed_at = _now()
    await db.flush()


#: The names the ``backend.services.identity`` package exports for this module:
#: unique across the package, so a caller outside it reads as what it does.
SSO_LINK_TTL = LINK_TTL
sso_link_view = view
sso_link_confirm = confirm
sso_link_cancel = cancel
sso_link_park = park


__all__ = [
    "LINK_TTL",
    "LinkResult",
    "LinkView",
    "SsoLinkError",
    "cancel",
    "confirm",
    "mask_email",
    "park",
    "token_hash",
    "view",
]
