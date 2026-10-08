"""Team membership domain operations.

Membership rule (locked decision): a user must be a member of every ancestor
team to be in a leaf. Adding a user to team B (under A under Org) writes
rows for B, A, and Org if missing. Removing a user from team A removes their
memberships in every team under A, and sheds each ancestor row above A that
nothing else in that ancestor's subtree still justifies (the org root is
always kept — it is the tenancy binding, not an access grant).

Owner and admin role assignments ride with the membership that justified them.
The founding admin's owner row is minted from the admin membership the org was
created with, and every owner/admin grant descends through the role resolver
exactly like an admin membership does, so a demotion or removal that left one
standing would change nothing the resolver reads. Demoting a membership below
admin, or removing it, revokes the user's live owner/admin grants scoped at
that team and every team beneath it, in the same transaction. Nothing re-grants
on a promotion: owner standing comes back only through an explicit assignment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.auth import revoke_membership
from alkera_core.authz import PrincipalKind, Role
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.files import drives
from alkera_core.models import OrgMembership, RoleAssignment, Team, TeamMembership, TeamRole, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import audit as audit_services
from backend.services.org import teams as team_service


class MembershipError(Exception):
    """Domain-level violation (e.g. removing the last org admin)."""


#: Named because the invitation flows re-raise it verbatim to the caller as a 409.
ALREADY_A_MEMBER = "User is already a member of this team"

#: The actor a membership change is recorded under when the caller names none
#: (SSO group mapping, SCIM provisioning, a federated first login).
SYSTEM_ACTOR = "backend:membership_service"

#: The grants a membership carries: the roles that descend through the resolver
#: the way an admin membership does. A member or viewer grant is an explicit,
#: non-descending grant no membership justifies, so no membership change touches it.
_MEMBERSHIP_BACKED_GRANTS: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN})


async def _revoke_admin_grants(
    db: AsyncSession, *, org_id: UUID, user_id: UUID, team_ids: Sequence[UUID]
) -> list[RoleAssignment]:
    """Revoke the user's live owner/admin grants scoped at any of ``team_ids``,
    inside the caller's transaction, and hand back the rows revoked.

    Revoked, never deleted: ``revoked_at`` keeps the row as the audit trail, and
    the resolver reads only live rows. Filtered on the org as well as the scope
    so a stray row this user holds under another org's tree is never reached.
    """
    if not team_ids:
        return []
    rows = list(
        (
            await db.execute(
                select(RoleAssignment).where(
                    RoleAssignment.org_team_id == org_id,
                    RoleAssignment.principal_kind == PrincipalKind.USER,
                    RoleAssignment.principal_id == user_id,
                    RoleAssignment.role.in_(list(_MEMBERSHIP_BACKED_GRANTS)),
                    RoleAssignment.revoked_at.is_(None),
                    RoleAssignment.scope_id.in_(list(team_ids)),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
    await db.flush()
    return rows


async def _emit_changed(
    db: AsyncSession,
    *,
    org_id: UUID,
    team_id: UUID,
    user_id: UUID,
    role: TeamRole,
    removed: bool,
    actor: Mapping[str, Any] | None,
) -> None:
    """Announce one membership row moving, inside the caller's transaction.
    The leaf row is what is announced; the ancestor rows a change
    materializes or sheds are its consequence, not separate news. The row
    names its team, so the stream hands it to that team's members and the
    org's admins."""
    await emit(
        db,
        org_id=org_id,
        type=EventType.MEMBERSHIP_CHANGED,
        entity=Entity.MEMBERSHIP,
        entity_id=f"{team_id}:{user_id}",
        payload={
            "team_id": str(team_id),
            "user_id": str(user_id),
            "role": role.value,
            "removed": removed,
        },
        actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
    )


async def list_for_team(db: AsyncSession, team_id: UUID) -> Sequence[TeamMembership]:
    result = await db.execute(
        select(TeamMembership)
        .where(TeamMembership.team_id == team_id)
        .order_by(TeamMembership.created_at)
    )
    return result.scalars().all()


async def list_for_user(
    db: AsyncSession, user_id: UUID, *, org_team_id: UUID
) -> Sequence[TeamMembership]:
    """Every team membership a user holds in one org. A person in several orgs
    holds rows in each; only the named org's are theirs to act on here."""
    result = await db.execute(
        select(TeamMembership)
        .where(TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_team_id)
        .order_by(TeamMembership.created_at)
    )
    return result.scalars().all()


async def org_admins(db: AsyncSession, *, org_id: UUID) -> list[User]:
    """The org-root ADMIN users, email-ordered -- who "ask an org admin" names
    on member-facing gate surfaces."""
    rows = await db.execute(
        select(User)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(
            TeamMembership.org_team_id == org_id,
            TeamMembership.team_id == org_id,
            TeamMembership.role == TeamRole.ADMIN,
        )
        .order_by(User.email)
    )
    return list(rows.scalars().all())


async def members_with_users(
    db: AsyncSession, *, team_ids: Sequence[UUID], org_team_id: UUID
) -> list[tuple[TeamMembership, User, Team]]:
    """Membership rows for `team_ids` in the org `org_team_id`, joined to the
    user identity + team name. Powers the enriched member list (per-team and
    org-wide). A team id from another org contributes nothing. Empty input → []."""
    ids = list(team_ids)
    if not ids:
        return []
    rows = await db.execute(
        select(TeamMembership, User, Team)
        .join(User, User.id == TeamMembership.user_id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(TeamMembership.org_team_id == org_team_id, TeamMembership.team_id.in_(ids))
        .order_by(Team.name, User.email)
    )
    return [(m, u, t) for m, u, t in rows.all()]


@dataclass(frozen=True, slots=True)
class Descent:
    """Admin reaching a team from above: the role carried and the nearest
    ancestor whose row carries it."""

    role: TeamRole
    from_team: Team


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One person's standing on one team, the two ways it can be held.

    ``direct_role`` is the row written on the team itself — None when the
    person reaches the team by descent alone. ``descent`` is admin reaching
    the team from a team above — None when nothing above grants anything here
    (a member row above grants nothing below). At least one is set.
    ``created_at`` is the direct row's, or the descent row's when there is no
    direct row.
    """

    user: User
    team: Team
    direct_role: TeamRole | None
    descent: Descent | None
    created_at: datetime

    @property
    def effective_role(self) -> TeamRole:
        """What the person holds here once descent is applied: admin when
        either side is admin, else the direct row's role."""
        if self.direct_role is TeamRole.ADMIN or (
            self.descent is not None and self.descent.role is TeamRole.ADMIN
        ):
            return TeamRole.ADMIN
        assert self.direct_role is not None
        return self.direct_role


async def _admins_above(
    db: AsyncSession, chain: Sequence[Team]
) -> dict[UUID, tuple[TeamMembership, User, Descent]]:
    """Every person an admin row on a strict ancestor of ``chain[0]`` reaches,
    keyed by user, attributed to the NEAREST ancestor holding such a row.
    Only admin descends; a member row above is not consulted."""
    ancestors = list(chain[1:])
    if not ancestors:
        return {}
    rows = await db.execute(
        select(TeamMembership, User, Team)
        .join(User, User.id == TeamMembership.user_id)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(
            TeamMembership.org_team_id == chain[-1].id,
            TeamMembership.team_id.in_([t.id for t in ancestors]),
            TeamMembership.role == TeamRole.ADMIN,
        )
    )
    by_team: dict[UUID, list[tuple[TeamMembership, User, Team]]] = {}
    for membership, user, team in rows.all():
        by_team.setdefault(team.id, []).append((membership, user, team))
    reached: dict[UUID, tuple[TeamMembership, User, Descent]] = {}
    # Nearest ancestor first, so the first row seen per user is the closest source.
    for ancestor in ancestors:
        for membership, user, team in by_team.get(ancestor.id, ()):
            reached.setdefault(user.id, (membership, user, Descent(TeamRole.ADMIN, team)))
    return reached


async def descent_for(db: AsyncSession, *, team_id: UUID, user_id: UUID) -> Descent | None:
    """What reaches ``user_id`` on ``team_id`` from above, or None. The fact a
    role write on the team is decided against."""
    chain = await team_service.ancestor_chain(db, team_id)
    if not chain:
        return None
    hit = (await _admins_above(db, chain)).get(user_id)
    return hit[2] if hit is not None else None


async def roster(db: AsyncSession, *, team_id: UUID) -> list[RosterEntry]:
    """Everyone who stands on ``team_id``: each person with a row on the team
    (their direct standing, with whatever descent also reaches them) and each
    person an admin row above reaches without a row here. Ordered by email.
    Empty when the team does not resolve."""
    chain = await team_service.ancestor_chain(db, team_id)
    if not chain:
        return []
    team = chain[0]
    above = await _admins_above(db, chain)
    entries: list[RosterEntry] = []
    seen: set[UUID] = set()
    for membership, user, _team in await members_with_users(
        db, team_ids=[team.id], org_team_id=chain[-1].id
    ):
        seen.add(user.id)
        hit = above.get(user.id)
        entries.append(
            RosterEntry(
                user=user,
                team=team,
                direct_role=membership.role,
                descent=hit[2] if hit is not None else None,
                created_at=membership.created_at,
            )
        )
    for user_id, (membership, user, descent) in above.items():
        if user_id in seen:
            continue
        entries.append(
            RosterEntry(
                user=user,
                team=team,
                direct_role=None,
                descent=descent,
                created_at=membership.created_at,
            )
        )
    entries.sort(key=lambda e: e.user.email)
    return entries


async def get(
    db: AsyncSession, *, team_id: UUID, user_id: UUID, org_team_id: UUID | None = None
) -> TeamMembership | None:
    """The user's row on ``team_id``. With ``org_team_id``, only when the team
    sits in that org (a team of another org answers None); without it, the org
    is the team's own, read from the tree."""
    if org_team_id is None:
        org_team_id = await team_service.org_root_id(db, team_id)
    return (
        await db.execute(
            select(TeamMembership).where(
                TeamMembership.org_team_id == org_team_id,
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def add_member(
    db: AsyncSession,
    *,
    team_id: UUID,
    user_id: UUID,
    role: TeamRole = TeamRole.MEMBER,
    actor: Mapping[str, Any] | None = None,
) -> TeamMembership:
    """Add user to `team_id` with `role`. Materializes membership in every
    ancestor (with role=MEMBER) so the chain rule holds. Announced on the
    event outbox under ``actor``.

    Returns the leaf membership row. If the leaf already exists, raises
    MembershipError (use `change_role` for upgrades). If only ancestors
    were missing (user was somehow in a subteam without ancestors), they
    get filled in silently — defensive.
    """
    chain = await team_service.ancestor_chain(db, team_id)
    if not chain:
        raise MembershipError("Team does not exist")
    # Serialize with the org's other membership/tree mutations so a concurrent
    # `remove_member` can't drop an ancestor row this call is relying on.
    org_id = chain[-1].id
    await team_service.lock_org_tree(db, org_id)

    if await get(db, team_id=team_id, user_id=user_id, org_team_id=org_id) is not None:
        raise MembershipError(ALREADY_A_MEMBER)

    leaf_membership: TeamMembership | None = None
    # Insert from root → leaf so foreign-key/business invariants are stable.
    for idx, team in enumerate(reversed(chain)):
        is_leaf = idx == len(chain) - 1
        existing = await get(db, team_id=team.id, user_id=user_id, org_team_id=org_id)
        effective_role = role if is_leaf else TeamRole.MEMBER
        if existing is None:
            membership = TeamMembership(user_id=user_id, team_id=team.id, role=effective_role)
            db.add(membership)
            await db.flush()
            await db.refresh(membership)
            if is_leaf:
                leaf_membership = membership
        elif is_leaf:
            # Shouldn't happen — covered by the early check above — but guard.
            leaf_membership = existing

    assert leaf_membership is not None
    if settings.files_enabled:
        # The member's home is part of joining, not a first-visit side effect:
        # a grant made before they ever open Files has somewhere to land.
        member = await db.get(User, user_id)
        if member is not None:
            ctx = ActingContext.for_user(user_id=member.id, org_id=chain[-1].id, email=member.email)
            async with team_service.files_transaction(db, ctx) as repo:
                await drives.ensure_home_folder(repo, ctx, member.id)
    await _emit_changed(
        db,
        org_id=chain[-1].id,
        team_id=team_id,
        user_id=user_id,
        role=role,
        removed=False,
        actor=actor,
    )
    return leaf_membership


async def change_role(
    db: AsyncSession,
    membership: TeamMembership,
    role: TeamRole,
    *,
    actor: Mapping[str, Any] | None = None,
) -> TeamMembership:
    """Change the row's role. A change to the role it already holds writes
    nothing and announces nothing.

    A demotion below admin also revokes the user's live owner/admin grants
    scoped at this team and every team beneath it (see the module docstring);
    a promotion restores none of them — an explicit assignment is required."""
    if membership.role is role:
        return membership
    org_id = await team_service.org_root_id(db, membership.team_id)
    demotion = membership.role is TeamRole.ADMIN and role is not TeamRole.ADMIN
    if demotion:
        # Same check-then-write as `remove_member`, so it needs the same lock:
        # two concurrent demotions of an org's last two root admins would each
        # see the other's ADMIN row, both pass, and both commit.
        await team_service.lock_org_tree(db, org_id)
        await _ensure_other_admin_exists(db, membership)
    membership.role = role
    await db.flush()
    # A privilege change ends every live credential of the member IN THIS ORG,
    # promotion or demotion: what they may do next must be decided under the new
    # role from the next request on, not once their current credentials lapse.
    # Their sessions in any other org are not this org's to end.
    org_membership = (
        await db.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == membership.user_id,
                OrgMembership.org_team_id == membership.org_team_id,
            )
        )
    ).scalar_one_or_none()
    if org_membership is not None:
        await revoke_membership(db, org_membership, reason="role_changed")
    await audit_services.record_security_event(
        db,
        user_id=membership.user_id,
        event="auth.org_role_changed",
        org_team_id=org_id,
        detail={"role": role.value, "scope": "org" if membership.team_id == org_id else "team"},
    )
    if demotion:
        await _revoke_admin_grants(
            db,
            org_id=org_id,
            user_id=membership.user_id,
            team_ids=[
                membership.team_id,
                *await team_service.descendant_ids(db, membership.team_id),
            ],
        )
    await _emit_changed(
        db,
        org_id=org_id,
        team_id=membership.team_id,
        user_id=membership.user_id,
        role=role,
        removed=False,
        actor=actor,
    )
    return membership


async def remove_member(
    db: AsyncSession, membership: TeamMembership, *, actor: Mapping[str, Any] | None = None
) -> None:
    """Remove the membership, every one of this user's memberships in a team
    UNDER `membership.team_id`, and every now-unjustified inherited row ABOVE
    it. Refuses if the membership is the last admin of an org root (existing
    rule).

    The upward sweep is the security-relevant half. `add_member` materializes a
    MEMBER row on every ancestor, and entitlement — shared team-connection
    credentials, KB scopes, billing pools — is granted purely by the presence of
    a row. Cascading only downward left a contractor removed from `Eng/Data`
    holding `Eng`'s row, and therefore `Eng`'s decrypted warehouse credential,
    after the admin believed they were gone.

    The schema can't distinguish a direct grant on an intermediate team from an
    inherited one (an `is_direct` column would), so this fails CLOSED: an
    ancestor MEMBER row survives only while the user still holds a membership
    somewhere in that ancestor's subtree. ADMIN rows are never auto-stripped,
    and the org root is never stripped — org membership is the user's tenancy
    binding, ended by deactivation, not by a team edit. Same posture as
    `team_service.reparent_team`'s membership repair.

    The user's live owner/admin role assignments scoped at `membership.team_id`
    or any team beneath it are revoked in the same transaction: they descend
    through the role resolver like the membership did, and a removed founder
    who kept the org's owner row would still pass every owner-gated decision.
    """
    user_id = membership.user_id
    team_id = membership.team_id
    removed_role = membership.role
    chain = await team_service.ancestor_chain(db, team_id)
    # The chain's root is the row's org; a chain that no longer resolves falls
    # back to the org the row itself is bound to.
    org_id = chain[-1].id if chain else membership.org_team_id
    # Lock BEFORE the last-admin probe, not after: the probe is itself a
    # check-then-write, and it is the one that most needs serializing. Two
    # concurrent removals of an org's last two root admins each saw the other's
    # ADMIN row, both passed, and both committed — leaving an org with zero
    # admins that no API path can repair, because restoring one requires an
    # existing org admin.
    await team_service.lock_org_tree(db, org_id)

    if membership.role is TeamRole.ADMIN:
        await _ensure_other_admin_exists(db, membership)

    descendants = await team_service.descendant_ids(db, team_id)
    if descendants:
        descendant_rows = await db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.org_team_id == org_id,
                TeamMembership.team_id.in_(descendants),
            )
        )
        for desc_membership in descendant_rows.scalars().all():
            await db.delete(desc_membership)

    await db.delete(membership)
    await db.flush()
    await _revoke_admin_grants(
        db,
        org_id=org_id,
        user_id=user_id,
        team_ids=[team_id, *descendants],
    )

    # Ancestors leaf-first: a row dropped here must already be gone when the
    # next (shallower) ancestor's justification query runs.
    for ancestor in chain[1:]:
        if ancestor.is_root:
            continue
        inherited = await get(db, team_id=ancestor.id, user_id=user_id, org_team_id=org_id)
        if inherited is None or inherited.role is TeamRole.ADMIN:
            continue
        remaining = await team_service.descendant_ids(db, ancestor.id)
        justified = (
            remaining
            and (
                await db.execute(
                    select(TeamMembership.id)
                    .where(
                        TeamMembership.user_id == user_id,
                        TeamMembership.org_team_id == org_id,
                        TeamMembership.team_id.in_(remaining),
                    )
                    .limit(1)
                )
            ).first()
        )
        if not justified:
            await db.delete(inherited)
            await db.flush()
    await _emit_changed(
        db,
        org_id=org_id,
        team_id=team_id,
        user_id=user_id,
        role=removed_role,
        removed=True,
        actor=actor,
    )


async def move_member(
    db: AsyncSession,
    *,
    user_id: UUID,
    from_team_id: UUID,
    to_team_id: UUID,
    role: TeamRole | None = None,
    actor: Mapping[str, Any] | None = None,
) -> TeamMembership:
    """Atomically move a user's membership from one team to another in the
    same org: remove from the source (cascading out of the source subtree),
    then add to the target (materializing the target's ancestor chain). The
    source role is preserved unless `role` overrides it.

    Raises `MembershipError` if the source membership is missing, the teams
    are identical, or removing the source would strip the last org admin
    (the existing `remove_member` guard).

    Caller enforces that both teams are in the same org and that the caller
    may admin both. Single unit of work — the route's `get_db` commits once.

    Ancestor note: `remove_member` now also sheds the source's unjustified
    inherited rows, so moving a member out of a NESTED source no longer leaves
    them inside the source's intermediate ancestors. A row the schema can't
    prove is a direct grant is dropped (fail-closed) and then re-materialized
    by the `add_member` below for every ancestor the TARGET shares.
    """
    if from_team_id == to_team_id:
        raise MembershipError("Source and target teams are the same")
    org_id = await team_service.org_root_id(db, from_team_id)
    if await team_service.org_root_id(db, to_team_id) != org_id:
        raise MembershipError("Source and target teams are in different organizations")
    source = await get(db, team_id=from_team_id, user_id=user_id, org_team_id=org_id)
    if source is None:
        raise MembershipError("User is not a member of the source team")
    effective_role = role if role is not None else source.role

    await remove_member(db, source, actor=actor)

    # The user may still sit in the target if it's an ancestor they kept;
    # upgrade that row's role rather than inserting a duplicate.
    existing = await get(db, team_id=to_team_id, user_id=user_id, org_team_id=org_id)
    if existing is not None:
        return await change_role(db, existing, effective_role, actor=actor)
    return await add_member(
        db, team_id=to_team_id, user_id=user_id, role=effective_role, actor=actor
    )


async def _ensure_other_admin_exists(db: AsyncSession, membership: TeamMembership) -> None:
    """For a root (org) team, refuse to drop below 1 Admin.

    Callers MUST already hold the org's tree lock (`team_service.lock_org_tree`)
    — this reads a count the caller is about to invalidate, so an unserialized
    call is a check-then-write two concurrent requests can both pass.
    """
    team = (
        await db.execute(select(Team).where(Team.id == membership.team_id))
    ).scalar_one_or_none()
    if team is None or not team.is_root:
        return
    # An existence probe, not a fetch: an org with three or more root admins is
    # ordinary (role=admin invitations, SSO group mapping), and asking for one
    # row back from a multi-row result raises instead of answering.
    other_admin = (
        await db.execute(
            select(TeamMembership.id)
            .where(
                TeamMembership.org_team_id == team.id,
                TeamMembership.team_id == membership.team_id,
                TeamMembership.role == TeamRole.ADMIN,
                TeamMembership.user_id != membership.user_id,
            )
            .limit(1)
        )
    ).first()
    if other_admin is None:
        raise MembershipError("Cannot remove the last org admin")
