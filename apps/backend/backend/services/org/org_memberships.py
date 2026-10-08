"""Org memberships: the rows that put an identity inside an org.

Read here, decided in ``alkera_core.auth.tenancy`` (the check every credential
door runs). A home membership is written by the database itself when a user is
created (``trg_users_home_membership``); :func:`create` is the one Python
writer, and it refuses an org that is not a root team.

Everything an org decides about a person acts on their membership, never on
the identity: :func:`deactivate`, :func:`reactivate` and :func:`remove` end or
restore the person's standing in ONE org and leave every other membership, the
identity row and its sessions elsewhere exactly as they were. The one write
to the identity is :func:`reactivate` in the person's home org lifting the
identity-level deactivation that org itself wrote before deactivation moved
onto the membership.

Nothing in this module lets a caller choose an org: every function takes the
org from code that already holds it (a credential, a row it owns).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.auth.revocation import bump_membership_epoch, revoke_membership
from alkera_core.auth.tenancy import LEGACY_MEMBERSHIP_EPOCH, home_org_id
from alkera_core.authz import PrincipalKind
from alkera_core.bans import banned_predicate
from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.logging import get_logger
from alkera_core.models import (
    IdentitySecurityEvent,
    MembershipStatus,
    OrgMembership,
    RoleAssignment,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from alkera_core.objects import chat_end
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import audit as audit_services
from backend.services.org import invitations as invitation_service
from backend.services.org import memberships as team_membership_service
from backend.services.org import removal_hooks

#: The actor a membership change is recorded under when the caller names none.
SYSTEM_ACTOR = "backend:org_memberships"

#: The epoch a membership starts at. A credential minted before memberships
#: existed names no epoch and is held to ``LEGACY_MEMBERSHIP_EPOCH``; only the
#: memberships back-filled for those credentials sit there. A membership
#: created since starts one past it, so a person who leaves an org and joins
#: it again never lands on an epoch an old credential of theirs still matches.
FIRST_MEMBERSHIP_EPOCH = LEGACY_MEMBERSHIP_EPOCH + 1

#: What platform staff record in the identity's security log when they disable
#: or re-enable it; the newest of the two says which the platform last did.
_PLATFORM_DISABLED = "platform.user_disabled"
_PLATFORM_ENABLED = "platform.user_enabled"

_log = get_logger(__name__)


class NotAnOrgError(ValueError):
    """A membership was asked for in a team that is not an org (a root team)."""


class LastActiveAdminError(ValueError):
    """The change would leave the org with no active admin. An org must always
    be able to administer itself, so no admin, IdP or API path may do it."""


async def get(db: AsyncSession, *, user_id: UUID, org_team_id: UUID) -> OrgMembership | None:
    """The membership of ``user_id`` in ``org_team_id``, whatever its status."""
    return (
        await db.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_team_id
            )
        )
    ).scalar_one_or_none()


async def active(db: AsyncSession, *, user_id: UUID, org_team_id: UUID) -> OrgMembership | None:
    """The membership of ``user_id`` in ``org_team_id`` when it is active."""
    membership = await get(db, user_id=user_id, org_team_id=org_team_id)
    if membership is None or membership.status is not MembershipStatus.ACTIVE:
        return None
    return membership


async def is_active_member(db: AsyncSession, *, user_id: UUID, org_id: UUID) -> bool:
    """Whether ``user_id`` holds an active membership in ``org_id``."""
    return await active(db, user_id=user_id, org_team_id=org_id) is not None


async def other_active_org(
    db: AsyncSession, *, user_id: UUID, org_team_id: UUID
) -> OrgMembership | None:
    """The oldest active membership ``user_id`` holds in an org other than
    ``org_team_id``, or None. The single-org rule reads it: an identity that
    already belongs somewhere else may not be seated in a second org."""
    for membership in await list_active_for_user(db, user_id):
        if membership.org_team_id != org_team_id:
            return membership
    return None


async def pending(db: AsyncSession, *, user_id: UUID, org_team_id: UUID) -> OrgMembership | None:
    """The membership of ``user_id`` in ``org_team_id`` when it is pending: an
    org provisioned it and the person has not joined yet."""
    membership = await get(db, user_id=user_id, org_team_id=org_team_id)
    if membership is None or membership.status is not MembershipStatus.PENDING:
        return None
    return membership


async def list_pending_for_user(db: AsyncSession, user_id: UUID) -> list[OrgMembership]:
    """Every pending membership of ``user_id``, oldest first."""
    rows = await db.execute(
        select(OrgMembership)
        .where(
            OrgMembership.user_id == user_id,
            OrgMembership.status == MembershipStatus.PENDING,
        )
        .order_by(OrgMembership.joined_at, OrgMembership.id)
    )
    return list(rows.scalars().all())


async def list_active_for_user(db: AsyncSession, user_id: UUID) -> list[OrgMembership]:
    """Every active membership of ``user_id``, oldest first."""
    rows = await db.execute(
        select(OrgMembership)
        .where(
            OrgMembership.user_id == user_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
        .order_by(OrgMembership.joined_at, OrgMembership.id)
    )
    return list(rows.scalars().all())


async def bump_epoch(db: AsyncSession, membership_id: UUID) -> int:
    """Retire every credential bound to this membership: the epoch they carry
    no longer matches. Returns the new epoch."""
    return await bump_membership_epoch(db, membership_id)


async def list_in_org(
    db: AsyncSession, org_team_id: UUID, *, active_only: bool = False
) -> Sequence[OrgMembership]:
    """The org's memberships, oldest first; only the active ones when asked."""
    stmt = select(OrgMembership).where(OrgMembership.org_team_id == org_team_id)
    if active_only:
        stmt = stmt.where(OrgMembership.status == MembershipStatus.ACTIVE)
    rows = await db.execute(stmt.order_by(OrgMembership.joined_at, OrgMembership.id))
    return rows.scalars().all()


async def members_of(
    db: AsyncSession,
    org_team_id: UUID,
    *,
    offset: int = 0,
    limit: int | None = None,
    sso_exempt_only: bool = False,
) -> list[tuple[User, OrgMembership]]:
    """Every person with an active or deactivated membership in the org, with
    that membership, in the order they joined the platform; one page of them
    when ``limit`` is given. "In the org" is the membership and nothing else:
    another org's members never appear, whatever their identity row says. A
    pending membership is left out: the person has not joined, so the org's
    rosters do not show them."""
    stmt = (
        select(User, OrgMembership)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(
            OrgMembership.org_team_id == org_team_id,
            OrgMembership.status != MembershipStatus.PENDING,
        )
    )
    if sso_exempt_only:
        stmt = stmt.where(OrgMembership.sso_exempt.is_(True))
    stmt = stmt.order_by(User.created_at, User.id).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).tuples().all())


async def org_ids_of(db: AsyncSession, user_id: UUID) -> list[UUID]:
    """Every org ``user_id`` holds a membership in, whatever its status."""
    rows = await db.execute(
        select(OrgMembership.org_team_id)
        .where(OrgMembership.user_id == user_id)
        .order_by(OrgMembership.joined_at, OrgMembership.id)
    )
    return list(rows.scalars().all())


async def is_org_admin(db: AsyncSession, membership: OrgMembership) -> bool:
    """Whether the membership's person is an admin of the org's root team."""
    row = await db.execute(
        select(TeamMembership.user_id).where(
            TeamMembership.org_team_id == membership.org_team_id,
            TeamMembership.team_id == membership.org_team_id,
            TeamMembership.user_id == membership.user_id,
            TeamMembership.role == TeamRole.ADMIN,
        )
    )
    return row.first() is not None


async def org_admin_ids(db: AsyncSession, *, org_id: UUID) -> set[UUID]:
    """User ids that are ADMIN of the org root team (and so, by permission
    descent, admin org-wide). Used to badge admins in the allocation view."""
    rows = (
        await db.execute(
            select(TeamMembership.user_id).where(
                TeamMembership.org_team_id == org_id,
                TeamMembership.team_id == org_id,
                TeamMembership.role == TeamRole.ADMIN,
            )
        )
    ).scalars()
    return set(rows)


async def other_active_admins(db: AsyncSession, membership: OrgMembership) -> int:
    """How many admins of the org's root team, other than this membership's
    person, hold an active membership in the org."""
    count = await db.scalar(
        select(func.count())
        .select_from(TeamMembership)
        .join(
            OrgMembership,
            and_(
                OrgMembership.user_id == TeamMembership.user_id,
                OrgMembership.org_team_id == TeamMembership.org_team_id,
            ),
        )
        .where(
            TeamMembership.org_team_id == membership.org_team_id,
            TeamMembership.team_id == membership.org_team_id,
            TeamMembership.role == TeamRole.ADMIN,
            TeamMembership.user_id != membership.user_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
    )
    return int(count or 0)


async def _refuse_stripping_the_last_admin(db: AsyncSession, membership: OrgMembership) -> None:
    if await is_org_admin(db, membership) and await other_active_admins(db, membership) == 0:
        raise LastActiveAdminError("the org would be left with no active admin")


async def _announce(
    db: AsyncSession,
    membership: OrgMembership,
    *,
    actor: Mapping[str, Any] | None,
    **payload: Any,
) -> None:
    """One ``MEMBERSHIP_CHANGED`` on the org's root, in the org's stream."""
    await emit(
        db,
        org_id=membership.org_team_id,
        type=EventType.MEMBERSHIP_CHANGED,
        entity=Entity.MEMBERSHIP,
        entity_id=f"{membership.org_team_id}:{membership.user_id}",
        payload={
            "team_id": str(membership.org_team_id),
            "user_id": str(membership.user_id),
            **payload,
        },
        actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
    )


async def _end_chats_in_org(
    db: AsyncSession, membership: OrgMembership, *, actor: Mapping[str, Any] | None
) -> None:
    """End the live chats the person runs in this org, and only in this org:
    nothing keeps serving a conversation for somebody the org has cut off,
    and nothing they run elsewhere stops."""
    await chat_end.end_chats(
        db,
        await chat_end.live_chats_owned_by(db, membership.user_id, org_id=membership.org_team_id),
        chat_end.ChatEndReason.ACCESS_REMOVED,
        actor=actor,
    )


async def deactivate(
    db: AsyncSession, membership: OrgMembership, *, actor: Mapping[str, Any] | None
) -> OrgMembership:
    """Offboard the person from this org, reversibly.

    The membership goes ``deactivated``, every credential the person holds in
    the org is refused from the next request on (:func:`revoke_membership`),
    their live chats in the org end, and the change is announced in the org.
    Their identity, its other memberships and every credential elsewhere are
    untouched. Refuses (:class:`LastActiveAdminError`) to deactivate the org's
    last active admin. A membership already deactivated writes nothing.

    A pending membership is withdrawn instead (:func:`withdraw_pending`): a
    deactivated row could later be reactivated, which would seat the person
    without their consent."""
    if membership.status is MembershipStatus.DEACTIVATED:
        return membership
    if membership.status is MembershipStatus.PENDING:
        await withdraw_pending(db, membership, actor=actor)
        return membership
    await _refuse_stripping_the_last_admin(db, membership)
    membership.status = MembershipStatus.DEACTIVATED
    membership.deactivated_at = datetime.now(UTC)
    await db.flush()
    await revoke_membership(db, membership, reason="deactivated")
    await _end_chats_in_org(db, membership, actor=actor)
    await removal_hooks.member_left(db, org_id=membership.org_team_id, user_id=membership.user_id)
    await _announce(db, membership, actor=actor, active=False)
    await audit_services.record_security_event(
        db,
        user_id=membership.user_id,
        event="auth.org_deactivated",
        org_team_id=membership.org_team_id,
    )
    return membership


async def _platform_refuses(db: AsyncSession, user_id: UUID) -> bool:
    """Whether the platform itself shuts ``user_id`` out: an active ban (by
    account or by email domain), or a platform disable that no platform
    re-enable has followed."""
    if await db.scalar(select(banned_predicate()).where(User.id == user_id)):
        return True
    latest = await db.scalar(
        select(IdentitySecurityEvent.event)
        .where(
            IdentitySecurityEvent.user_id == user_id,
            IdentitySecurityEvent.event.in_((_PLATFORM_DISABLED, _PLATFORM_ENABLED)),
        )
        .order_by(IdentitySecurityEvent.created_at.desc(), IdentitySecurityEvent.id.desc())
        .limit(1)
    )
    return latest == _PLATFORM_DISABLED


async def _lift_home_org_offboarding(db: AsyncSession, membership: OrgMembership) -> bool:
    """Re-enable an identity whose only disable is an offboarding its home org
    wrote before deactivation moved onto the membership. Returns whether it did.

    Until then an org deactivated a person by setting ``users.is_active`` to
    false, always in the person's home org, and there was no platform
    disable. Since then ``users.is_active`` is the platform's lever alone, and
    every platform disable is recorded in the identity's security log. So an
    inactive identity with no platform disable on record and no ban, restored
    by its home org, carries that org's own old decision, which the org may
    undo. Any other org, or an identity the platform disabled or banned,
    leaves the identity as it is."""
    user = await db.get(User, membership.user_id)
    if user is None or user.is_active or home_org_id(user) != membership.org_team_id:
        return False
    if await _platform_refuses(db, user.id):
        return False
    user.is_active = True
    await db.flush()
    _log.info(
        "org_membership.home_org_offboarding_lifted",
        org_id=str(membership.org_team_id),
        user_id=str(user.id),
    )
    return True


async def reactivate(
    db: AsyncSession, membership: OrgMembership, *, actor: Mapping[str, Any] | None
) -> OrgMembership:
    """Restore a deactivated membership. Nothing minted before the
    deactivation comes back (its epoch has moved on): the person signs in
    again for new credentials. A pending membership writes nothing: only the
    person activates it, by joining (:func:`activate_pending`).

    Restoring a person in their home org also lifts an identity-level
    deactivation that org wrote before deactivation moved onto the membership
    (:func:`_lift_home_org_offboarding`), whether or not the membership itself
    still reads deactivated. A platform disable or a ban is never lifted here.
    When neither the membership nor the identity changes, nothing is written."""
    if membership.status is MembershipStatus.PENDING:
        return membership
    lifted = await _lift_home_org_offboarding(db, membership)
    if membership.status is MembershipStatus.DEACTIVATED:
        membership.status = MembershipStatus.ACTIVE
        membership.deactivated_at = None
        await db.flush()
    elif not lifted:
        return membership
    await _announce(db, membership, actor=actor, active=True)
    await audit_services.record_security_event(
        db,
        user_id=membership.user_id,
        event="auth.org_reactivated",
        org_team_id=membership.org_team_id,
    )
    return membership


async def remove(
    db: AsyncSession, membership: OrgMembership, *, actor: Mapping[str, Any] | None
) -> None:
    """Take the person out of this org for good.

    Their credentials in the org are ended first, their pending invitations
    into the org are revoked, their live chats there end, and their live role
    assignments in the org are revoked (kept as history, read by nothing).
    Deleting the membership then removes every team seat they hold in the org
    (``team_memberships`` cascades from it). The identity is never deleted and
    no other org is touched. Refuses (:class:`LastActiveAdminError`) to remove
    the org's last active admin."""
    await _refuse_stripping_the_last_admin(db, membership)
    await revoke_membership(db, membership, reason="removed")
    await invitation_service.revoke_pending_by_inviter(
        db, membership.user_id, actor=actor, org_team_id=membership.org_team_id
    )
    await _end_chats_in_org(db, membership, actor=actor)
    await removal_hooks.member_left(db, org_id=membership.org_team_id, user_id=membership.user_id)
    await db.execute(
        update(RoleAssignment)
        .where(
            RoleAssignment.org_team_id == membership.org_team_id,
            RoleAssignment.principal_kind == PrincipalKind.USER,
            RoleAssignment.principal_id == membership.user_id,
            RoleAssignment.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    # The team seats in the org go with the membership (the composite foreign
    # key cascades); dropping them from the session keeps it from writing them
    # back on the next flush.
    for seat in [
        obj
        for obj in db.identity_map.values()
        if isinstance(obj, TeamMembership)
        and obj.user_id == membership.user_id
        and obj.org_team_id == membership.org_team_id
    ]:
        db.expunge(seat)
    await db.execute(
        delete(OrgMembership)
        .where(OrgMembership.id == membership.id)
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    if membership in db:
        db.expunge(membership)
    await _announce(db, membership, actor=actor, removed=True)


class NotPendingError(ValueError):
    """The membership is not pending, so there is nothing to join or withdraw."""


async def activate_pending(
    db: AsyncSession,
    membership: OrgMembership,
    *,
    actor: Mapping[str, Any] | None,
    seat_on_root: bool = True,
) -> OrgMembership:
    """The person joins the org that provisioned them: the pending membership
    goes active and they take a member's seat on the org's root team. The
    caller has already established that the person themselves asked, and
    that the org's sign-in policy admits them. An invitation's acceptance
    passes ``seat_on_root=False``: it seats the person on the invitation's own
    team chain, root included, with the invitation's role. Raises
    :class:`NotPendingError` for any other status."""
    if membership.status is not MembershipStatus.PENDING:
        raise NotPendingError(f"membership {membership.id} is {membership.status.value}")
    membership.status = MembershipStatus.ACTIVE
    membership.joined_at = datetime.now(UTC)
    await db.flush()
    if seat_on_root:
        await team_membership_service.add_member(
            db,
            team_id=membership.org_team_id,
            user_id=membership.user_id,
            role=TeamRole.MEMBER,
            actor=actor,
        )
    await _announce(db, membership, actor=actor, active=True)
    return membership


async def withdraw_pending(
    db: AsyncSession, membership: OrgMembership, *, actor: Mapping[str, Any] | None
) -> None:
    """Delete a pending membership. It never granted anything (no seat, no
    credential, no role), so the row is all there is to remove. Raises
    :class:`NotPendingError` for any other status."""
    if membership.status is not MembershipStatus.PENDING:
        raise NotPendingError(f"membership {membership.id} is {membership.status.value}")
    await db.execute(
        delete(OrgMembership)
        .where(
            OrgMembership.id == membership.id,
            OrgMembership.status == MembershipStatus.PENDING,
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    if membership in db:
        db.expunge(membership)
    await _announce(db, membership, actor=actor, removed=True)


async def create(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_team_id: UUID,
    status: MembershipStatus = MembershipStatus.ACTIVE,
    joined_at: datetime | None = None,
) -> OrgMembership:
    """Add ``user_id`` to the org ``org_team_id``, at
    :data:`FIRST_MEMBERSHIP_EPOCH`. Raises :class:`NotAnOrgError` when the team
    is missing or not a root team; an existing membership is an
    ``IntegrityError`` from the unique constraint."""
    team = await db.get(Team, org_team_id)
    if team is None or not team.is_root or team.parent_team_id is not None:
        raise NotAnOrgError(f"team {org_team_id} is not an org")
    membership = OrgMembership(
        user_id=user_id,
        org_team_id=org_team_id,
        status=status,
        credential_epoch=FIRST_MEMBERSHIP_EPOCH,
    )
    if joined_at is not None:
        membership.joined_at = joined_at
    db.add(membership)
    await db.flush()
    return membership


#: The names the ``backend.services.org`` package exports for this module:
#: unique across the package, so a caller outside it reads as what it does.
membership_in = get
active_membership_in = active
active_memberships_of = list_active_for_user
deactivate_membership = deactivate
reactivate_membership = reactivate
remove_from_org = remove
activate_pending_membership = activate_pending
withdraw_pending_membership = withdraw_pending
create_org_membership = create


__all__ = [
    "FIRST_MEMBERSHIP_EPOCH",
    "LastActiveAdminError",
    "NotAnOrgError",
    "NotPendingError",
    "activate_pending",
    "active",
    "bump_epoch",
    "create",
    "deactivate",
    "get",
    "is_active_member",
    "is_org_admin",
    "list_active_for_user",
    "list_in_org",
    "list_pending_for_user",
    "members_of",
    "org_admin_ids",
    "org_ids_of",
    "other_active_admins",
    "other_active_org",
    "pending",
    "reactivate",
    "remove",
    "withdraw_pending",
]
