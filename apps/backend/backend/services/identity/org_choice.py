"""Which orgs a signed-in person may enter, and which one a sign-in lands in.

One identity can belong to several orgs. This module answers the two questions
that follow, always about the person themselves and never about anyone else:

* the orgs they can switch into (``memberships_of``), each with their role and
  whether entering it takes the org's single sign-on;
* the org a sign-in that names none lands in (``landing_org``): the most
  recently used org whose sign-in policy admits the sign-in, falling back to
  the home org.

While multi-org is off (``multi_org_enabled()``), only the home org counts, so
a single-org person sees exactly what they saw before. Whether an org admits a
sign-in is always ``sign_in_policy.evaluate``'s answer; nothing here decides it.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from alkera_core.auth import sign_in_policy
from alkera_core.auth.tenancy import home_org_id, multi_org_enabled
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.tenant_session import bound_org_ids
from alkera_core.models import (
    MembershipStatus,
    OrgMembership,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from alkera_core.schemas.identity.membership import MembershipState, OrgRole
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import active_membership_in
from backend.services.org import membership_in as org_membership_in


@dataclass(frozen=True, slots=True)
class MembershipView:
    """One org the person belongs to, as the person sees it."""

    org_team_id: UUID
    org_name: str
    role: OrgRole
    sso_required: bool
    last_active_at: datetime | None
    status: MembershipState = "active"


@dataclass(frozen=True, slots=True)
class Landing:
    """Where a sign-in lands.

    ``org_team_id`` is the org the new session enters. ``choose_org`` asks the
    client to offer the chooser: the person has several orgs and the one they
    used last needs a step-up first. ``refusal`` is set when no org admits the
    sign-in at all; the caller answers with it as it always has."""

    org_team_id: UUID
    choose_org: bool = False
    refusal: sign_in_policy.StepUp | None = None


async def _active_rows(db: AsyncSession, user: User) -> list[tuple[OrgMembership, Team]]:
    """The person's active memberships with their org, most recently used
    first, then by name. Only the home org while multi-org is off."""
    stmt = (
        select(OrgMembership, Team)
        .join(Team, Team.id == OrgMembership.org_team_id)
        .where(
            OrgMembership.user_id == user.id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
        .order_by(
            OrgMembership.last_active_at.desc().nulls_last(),
            Team.name,
            OrgMembership.org_team_id,
        )
    )
    if not multi_org_enabled():
        stmt = stmt.where(OrgMembership.org_team_id == home_org_id(user))
    return list((await db.execute(stmt)).tuples().all())


async def enterable(db: AsyncSession, user: User, org_team_id: UUID) -> OrgMembership | None:
    """``user``'s active membership in ``org_team_id`` when they may enter it
    now, else None: no such org, not a member, a deactivated membership, and
    (while multi-org is off) any org but the home org all read the same, so a
    caller cannot tell an org that exists from one that does not."""
    if not multi_org_enabled() and org_team_id != home_org_id(user):
        return None
    return await active_membership_in(db, user_id=user.id, org_team_id=org_team_id)


async def _admin_orgs(db: AsyncSession, user_id: UUID, org_ids: Collection[UUID]) -> set[UUID]:
    """Which of ``org_ids`` ``user_id`` administers: an admin row on the org's
    root team.

    The rows live in ``team_memberships``, which a request's session sees only
    for the orgs it is bound to, so an org outside the binding would read as
    "not an admin". Those are read across tenants, and only the person's own
    rows: what the read can answer is exactly what the person may know about
    themselves."""
    if not org_ids:
        return set()
    stmt = select(TeamMembership.org_team_id).where(
        TeamMembership.user_id == user_id,
        TeamMembership.org_team_id.in_(list(org_ids)),
        TeamMembership.team_id == TeamMembership.org_team_id,
        TeamMembership.role == TeamRole.ADMIN,
    )
    bound = bound_org_ids(db)
    if bound is None or set(org_ids) <= set(bound):
        return set((await db.execute(stmt)).scalars().all())
    async with cross_tenant_write(db, reason="org_choice.own_admin_roles"):
        return set((await db.execute(stmt)).scalars().all())


async def role_in(db: AsyncSession, membership: OrgMembership) -> OrgRole:
    """The person's role in the membership's org, from the org's root team."""
    admin_of = await _admin_orgs(db, membership.user_id, [membership.org_team_id])
    return "admin" if membership.org_team_id in admin_of else "member"


async def sso_required_for(db: AsyncSession, user: User, membership: OrgMembership) -> bool:
    """Whether entering the membership's org takes a sign-in through its IdP:
    the connection is enabled and enforced, speaks for this person, and the
    org has not exempted them. Display only; entry itself is decided by
    ``sign_in_policy.evaluate``."""
    if user.platform_role is not None or membership.sso_exempt:
        return False
    connection = await sign_in_policy.connection_for(db, membership.org_team_id)
    if connection is None or not (connection.enabled and connection.enforced):
        return False
    return await sign_in_policy.governs(db, connection, user, membership)


async def _pending_rows(db: AsyncSession, user: User) -> list[tuple[OrgMembership, Team]]:
    """The person's pending memberships with their org, by name. None while
    multi-org is off."""
    if not multi_org_enabled():
        return []
    rows = await db.execute(
        select(OrgMembership, Team)
        .join(Team, Team.id == OrgMembership.org_team_id)
        .where(
            OrgMembership.user_id == user.id,
            OrgMembership.status == MembershipStatus.PENDING,
        )
        .order_by(Team.name, OrgMembership.org_team_id)
    )
    return list(rows.tuples().all())


async def memberships_of(db: AsyncSession, user: User) -> list[MembershipView]:
    """Every org ``user`` can switch into right now, most recently used first,
    then every org waiting for them to join (``status="pending"``; a pending
    membership holds no seat, so its role is ``member``)."""
    active = await _active_rows(db, user)
    admin_of = await _admin_orgs(db, user.id, [m.org_team_id for m, _team in active])
    views = [
        MembershipView(
            org_team_id=membership.org_team_id,
            org_name=team.name,
            role="admin" if membership.org_team_id in admin_of else "member",
            sso_required=await sso_required_for(db, user, membership),
            last_active_at=membership.last_active_at,
        )
        for membership, team in active
    ]
    for membership, team in await _pending_rows(db, user):
        views.append(
            MembershipView(
                org_team_id=membership.org_team_id,
                org_name=team.name,
                role="member",
                sso_required=await sso_required_for(db, user, membership),
                last_active_at=None,
                status="pending",
            )
        )
    return views


async def joinable(db: AsyncSession, user: User, org_team_id: UUID) -> OrgMembership | None:
    """``user``'s own pending membership in ``org_team_id``, or None: no such
    org, an active or deactivated membership, somebody else's, and multi-org
    being off all read the same."""
    if not multi_org_enabled():
        return None
    membership = await org_membership_in(db, user_id=user.id, org_team_id=org_team_id)
    if membership is None or membership.status is not MembershipStatus.PENDING:
        return None
    return membership


async def membership_count(db: AsyncSession, user: User) -> int:
    """How many orgs ``user`` can switch into (one while multi-org is off)."""
    return len(await _active_rows(db, user))


async def landing_org(db: AsyncSession, user: User, *, method: str) -> Landing:
    """The org a ``method`` sign-in that names no org lands in.

    Walks the person's orgs from the most recently used: the first whose
    policy admits the sign-in wins. When it is not the most recently used one,
    ``choose_org`` is set so the client can offer the chooser. When none admits
    it, the most recently used org's refusal comes back for the caller to
    answer with, exactly as a single-org sign-in is refused today. A person
    with no active membership at all lands in their home org, where the mint
    gives the refusal it always has."""
    rows = await _active_rows(db, user)
    if not rows:
        return Landing(org_team_id=home_org_id(user))
    first_refusal: sign_in_policy.StepUp | None = None
    for index, (membership, _team) in enumerate(rows):
        policy = await sign_in_policy.evaluate(
            db, user=user, org_team_id=membership.org_team_id, family_id=None, method=method
        )
        if isinstance(policy, sign_in_policy.Allowed):
            return Landing(org_team_id=membership.org_team_id, choose_org=index > 0)
        if first_refusal is None:
            first_refusal = policy
    return Landing(org_team_id=rows[0][0].org_team_id, refusal=first_refusal)


#: The code a registration answers with, while multi-org is on, for an address
#: that already has an account.
ACCOUNT_EXISTS_CODE = "account_exists"


def account_exists_detail(invite_token: str | None) -> dict[str, str]:
    """What a signup for an address that already has an account answers while
    multi-org is on: sign in instead, and keep the invitation the person came
    with. ``next`` is the sign-in path, carrying the caller's own invitation
    token back to them so the link survives the detour. It says nothing the
    refusal itself does not: that the address has an account."""
    next_path = "/login"
    if invite_token:
        next_path = f"/login?{urlencode({'invite': invite_token})}"
    return {
        "code": ACCOUNT_EXISTS_CODE,
        "message": "You already have an account. Sign in to continue.",
        "next": next_path,
    }


#: The names the ``backend.services.identity`` package exports for this module:
#: unique across the package, so a caller outside it reads as what it does.
enterable_org = enterable
joinable_org = joinable
org_sso_required_for = sso_required_for
org_landing = landing_org
org_membership_count = membership_count
org_role_in = role_in
switchable_orgs = memberships_of


__all__ = [
    "ACCOUNT_EXISTS_CODE",
    "Landing",
    "MembershipView",
    "account_exists_detail",
    "enterable",
    "joinable",
    "landing_org",
    "membership_count",
    "memberships_of",
    "role_in",
    "sso_required_for",
]
