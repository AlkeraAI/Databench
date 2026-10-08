"""What deleting one account would do, computed from live rows.

The plan answers, per org the person belongs to, whether the org keeps going
(``leave``), closes with them (``close``) or must wait for another admin
(``blocked``), who receives their shared items, and what still has to be
settled (live compute, a legal hold, and whatever an installed domain adds:
billing's renewing paid plan, see ``alkera_core.account.contributors``). The same function
serves the confirmation screen, the request, and the erasure's own re-check
when the grace window ends; nothing here writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.account.contributors import contributors
from alkera_core.authz.enums import PrincipalKind, Role
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.models import (
    MembershipStatus,
    OrgMembership,
    RoleAssignment,
    TeamMembership,
    TeamRole,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    PERSONAL_TENANCY,
    ComputeAllocation,
)
from alkera_core.models.files import FileHold
from alkera_core.models.org_machines import OrgMachine
from alkera_core.schemas.account import DeletionPlan, PlanBlocker, PlannedOrg

#: The ``visibility_scope`` of an item only its owner can see.
PRIVATE_SCOPE = "private"

#: Memberships that count as "someone else is in this org". A pending seat is an
#: invitation the person never took up, so it neither keeps an org open nor
#: needs an admin.
_STANDING = (MembershipStatus.ACTIVE, MembershipStatus.DEACTIVATED)


async def _orgs_of(db: AsyncSession, user_id: uuid.UUID) -> list[OrgMembership]:
    rows = await db.execute(
        select(OrgMembership)
        .where(OrgMembership.user_id == user_id, OrgMembership.status.in_(_STANDING))
        .order_by(OrgMembership.joined_at, OrgMembership.id)
    )
    return list(rows.scalars().all())


async def _others_in(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(OrgMembership)
        .where(
            OrgMembership.org_team_id == org_id,
            OrgMembership.user_id != user_id,
            OrgMembership.status.in_(_STANDING),
        )
    )
    return int(count or 0)


async def _active_admins(db: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    """The org's active root admins, longest-standing first."""
    rows = await db.execute(
        select(TeamMembership.user_id)
        .join(
            OrgMembership,
            and_(
                OrgMembership.user_id == TeamMembership.user_id,
                OrgMembership.org_team_id == TeamMembership.org_team_id,
            ),
        )
        .join(User, User.id == TeamMembership.user_id)
        .where(
            TeamMembership.org_team_id == org_id,
            TeamMembership.team_id == org_id,
            TeamMembership.role == TeamRole.ADMIN,
            OrgMembership.status == MembershipStatus.ACTIVE,
            User.deleted_at.is_(None),
        )
        .order_by(OrgMembership.joined_at, OrgMembership.id)
    )
    return list(rows.scalars().all())


async def _owner_holders(db: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(RoleAssignment.principal_id).where(
            RoleAssignment.org_team_id == org_id,
            RoleAssignment.principal_kind == PrincipalKind.USER,
            RoleAssignment.role == Role.OWNER,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    return list(rows.scalars().all())


async def _oldest_active_member(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID | None:
    found: uuid.UUID | None = await db.scalar(
        select(OrgMembership.user_id)
        .join(User, User.id == OrgMembership.user_id)
        .where(
            OrgMembership.org_team_id == org_id,
            OrgMembership.user_id != user_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
            User.deleted_at.is_(None),
        )
        .order_by(OrgMembership.joined_at, OrgMembership.id)
        .limit(1)
    )
    return found


async def transfer_recipient(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID | None:
    """Who takes the person's shared items in an org that keeps going: the
    active admin holding the org's owner role, else the longest-standing other
    active admin, else (an org with no active admin at all) its
    longest-standing active member. ``None`` when nobody else is active."""
    admins = [a for a in await _active_admins(db, org_id) if a != user_id]
    owners = set(await _owner_holders(db, org_id))
    for admin in admins:
        if admin in owners:
            return admin
    if admins:
        return admins[0]
    return await _oldest_active_member(db, org_id, user_id)


async def shared_object_ids(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> set[uuid.UUID]:
    """The live workspace objects the person owns in the org that somebody else
    can reach: published to a team or the org (``visibility_scope``), or whose
    folder's effective ACL names any principal other than the owner. A chat is
    created private and shared through its folder, so the scope alone would
    read every chat as private."""
    rows = await db.execute(
        text(
            """
            SELECT o.id FROM workspace_objects o
            WHERE o.org_team_id = :org AND o.owner_user_id = :uid AND o.deleted_at = 0
              AND (
                o.visibility_scope <> :private
                OR EXISTS (
                  SELECT 1 FROM file_nodes n
                  JOIN file_acl_members m ON m.acl_id = n.acl_id
                  WHERE n.org_team_id = o.org_team_id
                    AND n.target_object_id = o.id
                    AND n.trashed_at IS NULL
                    AND NOT (m.principal_kind = 'user' AND m.principal_id = o.owner_user_id)
                )
              )
            """
        ),
        {"org": org_id, "uid": user_id, "private": PRIVATE_SCOPE},
    )
    return set(rows.scalars().all())


async def _item_counts(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> tuple[int, int]:
    """``(shared, private)`` live items the person owns in the org."""
    total_objects = int(
        await db.scalar(
            select(func.count())
            .select_from(WorkspaceObject)
            .where(
                WorkspaceObject.org_team_id == org_id,
                WorkspaceObject.owner_user_id == user_id,
                WorkspaceObject.deleted_at == 0,
            )
        )
        or 0
    )
    shared = len(await shared_object_ids(db, org_id, user_id))
    private = total_objects - shared
    for contributor in contributors():
        if contributor.owned_counts is not None:
            more_shared, more_private = await contributor.owned_counts(db, org_id, user_id)
            shared += more_shared
            private += more_private
    return shared, private


async def _live_compute(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Orgs where the person has a machine allocated that is not yet off the
    plane. A box on their own hardware is not compute we bill or run: its
    credential is revoked at erasure instead. An org machine's allocation is
    the org's, whoever started it: it stays with an org that keeps going, and
    an org that closes is held by its machines instead."""
    rows = await db.execute(
        select(ComputeAllocation.org_team_id)
        .where(
            ComputeAllocation.user_id == user_id,
            ComputeAllocation.state.not_in(COMPUTE_TERMINAL_STATES),
            ComputeAllocation.tenancy != PERSONAL_TENANCY,
            ComputeAllocation.org_machine_id.is_(None),
        )
        .distinct()
    )
    return list(rows.scalars().all())


async def _has_org_machines(db: AsyncSession, org_id: uuid.UUID) -> bool:
    found = await db.scalar(select(OrgMachine.id).where(OrgMachine.org_team_id == org_id).limit(1))
    return found is not None


async def _has_live_hold(db: AsyncSession, org_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(FileHold.id)
        .where(FileHold.org_team_id == org_id, FileHold.released_at.is_(None))
        .limit(1)
    )
    return found is not None


async def compute_plan(
    db: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> DeletionPlan:
    """The deletion plan for ``user_id`` as the rows stand now.

    Runs inside a cross-tenant window: the person's orgs are many, and a read
    held to one tenant would silently see only that one's items."""
    async with cross_tenant_write(db, reason="account.plan"):
        user = await db.get(User, user_id)
        if user is None:
            raise LookupError(f"no user {user_id}")
        blockers: list[PlanBlocker] = []
        if user.platform_role is not None:
            blockers.append(PlanBlocker(code="platform_staff"))
        orgs: list[PlannedOrg] = []
        for membership in await _orgs_of(db, user_id):
            org_id = membership.org_team_id
            shared, private = await _item_counts(db, org_id, user_id)
            if await _others_in(db, org_id, user_id) == 0:
                orgs.append(
                    PlannedOrg(
                        org_id=org_id, fate="close", shared_items=shared, private_items=private
                    )
                )
                if await _has_org_machines(db, org_id):
                    blockers.append(PlanBlocker(code="org_machines", org_id=org_id))
                if await _has_live_hold(db, org_id):
                    blockers.append(PlanBlocker(code="legal_hold", org_id=org_id))
                continue
            is_admin = user_id in await _active_admins(db, org_id)
            other_admins = [a for a in await _active_admins(db, org_id) if a != user_id]
            recipient = await transfer_recipient(db, org_id, user_id)
            if (is_admin and not other_admins) or recipient is None:
                orgs.append(
                    PlannedOrg(
                        org_id=org_id, fate="blocked", shared_items=shared, private_items=private
                    )
                )
                blockers.append(PlanBlocker(code="last_admin", org_id=org_id))
                continue
            orgs.append(
                PlannedOrg(
                    org_id=org_id,
                    fate="leave",
                    transfer_to_user_id=recipient,
                    shared_items=shared,
                    private_items=private,
                )
            )
        for org_id in await _live_compute(db, user_id):
            blockers.append(PlanBlocker(code="live_compute", org_id=org_id))
        forfeited = 0
        for contributor in contributors():
            if contributor.blockers is not None:
                blockers.extend(await contributor.blockers(db, user_id))
            if contributor.forfeited_nanos is not None:
                forfeited += await contributor.forfeited_nanos(db, user_id)
    return DeletionPlan(
        computed_at=now or datetime.now(UTC),
        orgs=orgs,
        blockers=blockers,
        forfeited_credit_nanos=forfeited,
    )


__all__ = [
    "PRIVATE_SCOPE",
    "compute_plan",
    "shared_object_ids",
    "transfer_recipient",
]
