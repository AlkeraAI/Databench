"""The membership check every credential door runs.

A request's org comes from its credential and from nowhere else. Every
credential that reaches tenant data names a user and an org, and new ones also
name the membership they were minted for (``mid``) and that membership's
credential epoch at mint (``mep``). :func:`verify_membership` is the one
answer to "does this credential still stand in this org": the membership
exists, belongs to the credential's user, is active, and (for a credential
that names one) is the very membership it names at the epoch it names.

A credential minted before memberships existed names no membership. It is
resolved by (user, org) and counts as epoch 0, which is what every membership
was back-filled at, so every live token kept working; the first bump of the
membership's epoch retires it, which is what a bump is for.

Lives in ``api-core`` so the backend, the model gateway and the realtime
socket run the same check.

While :func:`multi_org_enabled` is False, an identity may hold a credential
for its home org only (the single-org guard): every door refuses an org that
is not ``users.org_team_id`` (``User.home_org_team_id``).
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import Select, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.tokens import SessionClaims
from alkera_core.config import get_settings
from alkera_core.models import MembershipStatus, OrgMembership, User

#: The 401 code a credential whose membership no longer stands is answered
#: with, on every door: the API, the gateway, the socket, the event stream,
#: the refresh route and the device grant.
SESSION_ORG_REVOKED: Final = "session_org_revoked"

#: The header a response echoes the request's org in, and a client asserts the
#: org it believes it is in. An assertion is compared with the credential's
#: org and a mismatch refused; it never selects an org.
ORG_HEADER: Final = "X-Alkera-Org"

#: What a credential that names no membership is taken to have been minted at.
LEGACY_MEMBERSHIP_EPOCH: Final = 0


class MembershipRefused(Exception):  # noqa: N818 - the name is the contract other code imports
    """The credential's membership does not stand. ``code`` is the error code
    every door answers with; the message says why, for logs only."""

    def __init__(
        self, code: str = SESSION_ORG_REVOKED, message: str = "membership refused"
    ) -> None:
        super().__init__(message)
        self.code = code


def multi_org_enabled() -> bool:
    """Whether an identity may hold a credential for an org other than its home
    org. Read per call so a test can turn it on for itself alone."""
    return get_settings().multi_org_enabled


async def stands_in(db: AsyncSession, user: User, org_team_id: UUID) -> bool:
    """Whether ``user`` may act in ``org_team_id`` right now: the identity is
    not platform-disabled and holds an active membership there. The check for
    a path that resolves a person some other way than a credential (a Slack
    link, an invitation's inviter) and must hold them to the same standing
    every credential door does."""
    if not user.is_active:
        return False
    row = await db.execute(
        select(OrgMembership.id)
        .where(
            OrgMembership.user_id == user.id,
            OrgMembership.org_team_id == org_team_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
        .limit(1)
    )
    return row.first() is not None


def home_org_id(user: User) -> UUID:
    """The identity's home org: the org a sign-in enters when it names none.

    Until a sign-in can choose among the person's orgs, every password, OAuth
    and SSO sign-in enters this one, and the single-org guard admits no other.
    It is not the org of a request (a request's org is its credential's,
    ``CurrentOrg``) and never the answer to "which org does this person act
    in"; the reads of the identity column that remain go through here so they
    stay in one place."""
    return user.home_org_team_id


#: The session setting that admits a change of ``users.org_team_id`` for one
#: transaction (``trg_users_home_org_immutable`` refuses it otherwise).
HOME_REPOINT_SETTING: Final = "alkera.allow_home_repoint"


def users_homed_in(org_team_id: UUID) -> Select[tuple[User]]:
    """The identities whose home org is ``org_team_id``: what an org's deletion
    has to move or delete before the org row can go."""
    return select(User).where(User.home_org_team_id == org_team_id).order_by(User.id)


async def repoint_home(db: AsyncSession, user: User, org_team_id: UUID) -> None:
    """Move ``user``'s home to ``org_team_id``, one of their other memberships.

    Only an org's deletion does this, for a person who belongs elsewhere: the
    trigger that guards the column is opened for this statement alone
    (``SET LOCAL``, closed again right after), so no other writer can move a
    home org."""
    await db.execute(text("SELECT set_config(:name, 'on', true)"), {"name": HOME_REPOINT_SETTING})
    try:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values({User.home_org_team_id: org_team_id})
            .execution_options(synchronize_session=False)
        )
    finally:
        await db.execute(
            text("SELECT set_config(:name, 'off', true)"), {"name": HOME_REPOINT_SETTING}
        )
    user.home_org_team_id = org_team_id
    await db.flush()


def assert_single_org(user: User, *, org_team_id: UUID) -> None:
    """The single-org guard: with multi-org off, a credential for any org but
    ``user``'s home org is refused, whatever the membership table says. With
    multi-org on it admits every org, and the membership check that follows it
    on every door is the whole answer."""
    if not multi_org_enabled() and org_team_id != home_org_id(user):
        raise MembershipRefused(message="multi-org is off and the org is not the home org")


async def _membership(db: AsyncSession, *, user_id: UUID, org_team_id: UUID) -> OrgMembership:
    # populate_existing: a session that already holds the row must not answer
    # from what it read before a deactivation or an epoch bump.
    membership = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_team_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise MembershipRefused(message="no membership in the credential's org")
    if membership.status is not MembershipStatus.ACTIVE:
        raise MembershipRefused(message="the membership is not active")
    return membership


async def require_active_membership(
    db: AsyncSession, *, user_id: UUID, org_team_id: UUID
) -> OrgMembership:
    """The active membership of ``user_id`` in ``org_team_id``, at whatever its
    current epoch is, or :class:`MembershipRefused`. For MINTING a credential
    (which then carries that epoch) and for a refresh family, which names an
    org but no epoch; a presented credential goes through
    :func:`verify_membership` instead."""
    return await _membership(db, user_id=user_id, org_team_id=org_team_id)


async def verify_membership(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_team_id: UUID,
    membership_id: UUID | None,
    membership_epoch: int | None,
) -> OrgMembership:
    """The active membership a presented credential stands on, or
    :class:`MembershipRefused`.

    Refused when there is no membership of ``user_id`` in ``org_team_id``, when
    it is not active, and when the credential names a membership (``mid``) that
    is not this one or an epoch (``mep``) that is not its current epoch. A
    credential that names no epoch was minted before memberships existed and is
    held to epoch 0.
    """
    membership = await _membership(db, user_id=user_id, org_team_id=org_team_id)
    if membership_id is not None and membership.id != membership_id:
        raise MembershipRefused(message="the credential names another membership")
    expected = LEGACY_MEMBERSHIP_EPOCH if membership_epoch is None else membership_epoch
    if membership.credential_epoch != expected:
        raise MembershipRefused(message="the membership's credential epoch has moved")
    return membership


async def verify_claims(db: AsyncSession, claims: SessionClaims, *, user: User) -> OrgMembership:
    """The whole door check for a session-shaped credential (a session or CLI
    token, a socket ticket's session, a gateway token's parent): the
    single-org guard against the identity's home org, then
    :func:`verify_membership` with the org, membership and epoch it names."""
    assert_single_org(user, org_team_id=claims.org_team_id)
    return await verify_membership(
        db,
        user_id=claims.user_id,
        org_team_id=claims.org_team_id,
        membership_id=claims.membership_id,
        membership_epoch=claims.membership_epoch,
    )


async def claims_stand(db: AsyncSession, claims: SessionClaims, *, user: User) -> bool:
    """:func:`verify_claims` as a yes or no, for a held connection's tick."""
    try:
        await verify_claims(db, claims, user=user)
    except MembershipRefused:
        return False
    return True


async def membership_stands(db: AsyncSession, claims: SessionClaims) -> bool:
    """Whether the membership ``claims`` names still stands in the org it is
    for (:func:`verify_membership` as a yes or no): for a held connection
    re-checking itself between ticks, where the single-org guard was already
    applied at its door."""
    try:
        await verify_membership(
            db,
            user_id=claims.user_id,
            org_team_id=claims.org_team_id,
            membership_id=claims.membership_id,
            membership_epoch=claims.membership_epoch,
        )
    except MembershipRefused:
        return False
    return True


async def member_stands(db: AsyncSession, *, user: User, org_team_id: UUID) -> bool:
    """Whether ``user`` may act in ``org_team_id`` right now, for a door that
    holds no credential of the person's own: a Slack workspace acting for the
    person it is linked to, a share made to them from a Slack button. The org
    is the door's (the workspace's binding), never the person's; the answer is
    the single-org guard against the identity's home org, then
    :func:`stands_in` (not platform-disabled, an active membership there)."""
    try:
        assert_single_org(user, org_team_id=org_team_id)
    except MembershipRefused:
        return False
    return await stands_in(db, user, org_team_id)


__all__ = [
    "HOME_REPOINT_SETTING",
    "LEGACY_MEMBERSHIP_EPOCH",
    "ORG_HEADER",
    "SESSION_ORG_REVOKED",
    "MembershipRefused",
    "assert_single_org",
    "claims_stand",
    "home_org_id",
    "member_stands",
    "membership_stands",
    "multi_org_enabled",
    "repoint_home",
    "require_active_membership",
    "stands_in",
    "users_homed_in",
    "verify_claims",
    "verify_membership",
]
