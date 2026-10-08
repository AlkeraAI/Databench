"""Team domain operations + transactional org bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from alkera_core.auth.revocation import revoke_memberships
from alkera_core.auth.tenancy import repoint_home, users_homed_in
from alkera_core.authz import CredentialKind, PrincipalKind, Role, ScopeKind
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.files import drives
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.logging import get_logger
from alkera_core.models import (
    IdentityOrgCreation,
    MembershipStatus,
    OrgMembership,
    OrgSettings,
    PlatformRole,
    RoleAssignment,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from alkera_core.org_entitlements import org_entitlements
from sqlalchemy import CTE, delete, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services.identity import users as user_service
from backend.services.org import creation_hooks, removal_hooks

log = get_logger(__name__)

#: Hard bound on every tree walk below. The walks are recursive SQL, so a
#: malformed row (a `parent_team_id` cycle) costs a bounded number of rows and
#: yields a truncated answer instead of spinning forever inside the event loop.
_MAX_WALK_DEPTH = 64

#: Per-org structural caps. Creating a team is a ~100-byte authenticated POST
#: that INSERTs one row; with no ceiling a single tenant could grow the table
#: without bound and slow every tenancy walk in the platform.
MAX_TEAMS_PER_ORG = 500
MAX_TEAM_DEPTH = 16


class TeamConflictError(Exception):
    """Operation rejected because of a structural rule (e.g. deleting root)."""


async def get_by_id(db: AsyncSession, team_id: UUID) -> Team | None:
    return (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()


async def lock_org_tree(db: AsyncSession, org_team_id: UUID) -> None:
    """Serialize structural mutations of ONE org's team tree.

    The cycle guard in `reparent_team` (and the ancestor repair in
    `membership_service`) are read-then-write sequences: without a lock two
    concurrent moves each see a tree without the other's edit, both commit, and
    the result is `A.parent = B, B.parent = A` — a cycle no walker can escape
    and no API call can repair. A transaction-scoped advisory lock keyed on the
    org root makes check-then-write atomic per tenant without touching
    cross-tenant throughput. Released on the caller's commit/rollback.
    """
    await advisory_xact_lock(db, advisory_key("team-tree", org_team_id))


async def org_root_id(db: AsyncSession, team_id: UUID) -> UUID:
    """The org root `team_id` hangs under — `team_id` itself if the chain can't
    be walked. Used to key the per-org tree lock."""
    chain = await ancestor_chain(db, team_id)
    return chain[-1].id if chain else team_id


def _ancestor_cte(team_id: UUID) -> CTE:
    """Recursive walk UP from `team_id`, depth-bounded."""
    anchor = (
        select(
            Team.id.label("id"),
            Team.parent_team_id.label("parent_team_id"),
            literal(0).label("depth"),
        )
        .where(Team.id == team_id)
        .cte("team_ancestors", recursive=True)
    )
    parent = aliased(Team)
    return anchor.union_all(
        select(parent.id, parent.parent_team_id, anchor.c.depth + 1).where(
            parent.id == anchor.c.parent_team_id,
            anchor.c.depth < _MAX_WALK_DEPTH,
        )
    )


def _descendant_cte(team_id: UUID) -> CTE:
    """Recursive walk DOWN from `team_id` (excludes the anchor), depth-bounded."""
    anchor = (
        select(Team.id.label("id"), literal(0).label("depth"))
        .where(Team.parent_team_id == team_id)
        .cte("team_descendants", recursive=True)
    )
    child = aliased(Team)
    return anchor.union_all(
        select(child.id, anchor.c.depth + 1).where(
            child.parent_team_id == anchor.c.id,
            anchor.c.depth < _MAX_WALK_DEPTH,
        )
    )


async def ancestor_chain(db: AsyncSession, team_id: UUID) -> list[Team]:
    """Return [team, parent, grandparent, ..., org_root]. Leaf-first.

    Empty list if `team_id` doesn't resolve. Used by:
      - require_team_admin (walk up to find an admin row)
      - membership chain materialization (insert from root down)
      - invitation acceptance (same)

    Walks in SQL from `team_id` upward, so one tenant's request never loads
    another tenant's teams, and stops at the first repeated id so a cycle
    yields a truncated chain rather than an unbounded loop.
    """
    cte = _ancestor_cte(team_id)
    rows = (
        await db.execute(
            select(Team, cte.c.depth).join(cte, Team.id == cte.c.id).order_by(cte.c.depth)
        )
    ).all()
    chain: list[Team] = []
    seen: set[UUID] = set()
    for team, _depth in rows:
        if team.id in seen:
            break
        seen.add(team.id)
        chain.append(team)
    return chain


async def belongs_to_org(db: AsyncSession, team_id: UUID, org_team_id: UUID) -> bool:
    """True iff `team_id` is `org_team_id` itself or sits anywhere beneath it in
    the tree. Walks the full ancestor chain (ANY depth), so it works for
    grandchild teams — the canonical "is this team in the caller's org?" check
    shared by the teams + memberships routes. False for a team that doesn't
    resolve (empty chain)."""
    chain = await ancestor_chain(db, team_id)
    return any(t.id == org_team_id for t in chain)


async def descendant_ids(db: AsyncSession, team_id: UUID) -> list[UUID]:
    """Every team id strictly below `team_id` in the tree (excludes the team itself).

    Returned in BFS order so `delete_team` and membership cascades can act
    leaf-first. Walks down in SQL (depth-bounded, de-duplicated) so a cycle
    can't make it enumerate forever.
    """
    cte = _descendant_cte(team_id)
    rows = (await db.execute(select(cte.c.id).order_by(cte.c.depth))).scalars().all()
    out: list[UUID] = []
    seen: set[UUID] = {team_id}
    for tid in rows:
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


async def admin_team_ids(db: AsyncSession, user_id: UUID, *, org_team_id: UUID) -> set[UUID]:
    """Every team this user administers in ``org_team_id``, descent included.

    An ADMIN membership row exists only where the role was actually granted —
    joining a sub-team materializes MEMBER rows on its ancestors, never ADMIN —
    so admin-ness is the granted teams plus everything below them. This is the
    set form of ``_is_admin_of_team_or_ancestor``, answered once for a whole
    page instead of one ancestor walk per row.

    One org only: a person who administers a team in another org of theirs
    administers nothing here, so the org is the request's, never inferred.
    """
    granted = (
        (
            await db.execute(
                select(TeamMembership.team_id).where(
                    TeamMembership.user_id == user_id,
                    TeamMembership.org_team_id == org_team_id,
                    TeamMembership.role == TeamRole.ADMIN,
                )
            )
        )
        .scalars()
        .all()
    )
    reachable: set[UUID] = set(granted)
    for team_id in granted:
        reachable.update(await descendant_ids(db, team_id))
    return reachable


async def member_counts(db: AsyncSession, team_ids: Sequence[UUID]) -> dict[UUID, int]:
    """Direct membership-row count per team (includes inherited/materialized
    rows, so an intermediate team's count = everyone in its subtree). Drives
    the tree's member badges and the empty-team delete gate. Empty input → {}.
    """
    ids = list(team_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(TeamMembership.team_id, func.count())
        .where(TeamMembership.team_id.in_(ids))
        .group_by(TeamMembership.team_id)
    )
    return {team_id: count for team_id, count in rows.all()}


async def list_in_org(db: AsyncSession, org_team_id: UUID) -> Sequence[Team]:
    """Every team in the same org tree as `org_team_id` (the root), root first.

    Walks DOWN from the root in SQL. The previous shape loaded every team row
    of every tenant and filtered in Python — so one org's rows were a cost (and
    a cycle in one org's rows an infinite loop) on every other org's request.
    """
    ids = [org_team_id, *await descendant_ids(db, org_team_id)]
    rows = await db.execute(select(Team).where(Team.id.in_(ids)).order_by(Team.created_at))
    return rows.scalars().all()


async def count_in_org(db: AsyncSession, org_team_id: UUID) -> int:
    """Team count for the org tree rooted at `org_team_id` (root included)."""
    cte = _descendant_cte(org_team_id)
    total = (await db.execute(select(func.count()).select_from(cte))).scalar_one()
    return int(total) + 1


async def list_all_roots(db: AsyncSession) -> Sequence[Team]:
    """Every org root team (admin route)."""
    result = await db.execute(select(Team).where(Team.is_root.is_(True)).order_by(Team.created_at))
    return result.scalars().all()


async def create_subteam(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    name: str,
    parent_team_id: UUID | None = None,
) -> Team:
    """Create a non-root team under `parent_team_id` (defaults to the org
    root). Caller must enforce permissions before calling.

    Bounded: an org may hold at most `MAX_TEAMS_PER_ORG` teams nested at most
    `MAX_TEAM_DEPTH` deep. Every tenancy check in the platform walks this tree,
    so an unbounded one is a self-service denial-of-service.
    """
    parent_id = parent_team_id or org_team_id
    parent = await get_by_id(db, parent_id)
    if parent is None:
        raise TeamConflictError("Parent team does not exist")
    chain = await ancestor_chain(db, parent_id)
    root_id = chain[-1].id if chain else parent_id
    await lock_org_tree(db, root_id)
    if len(chain) >= MAX_TEAM_DEPTH:
        raise TeamConflictError(f"Teams cannot nest more than {MAX_TEAM_DEPTH} levels deep")
    if await count_in_org(db, root_id) >= MAX_TEAMS_PER_ORG:
        raise TeamConflictError(
            f"This organization already has the maximum of {MAX_TEAMS_PER_ORG} teams"
        )
    team = Team(name=name, parent_team_id=parent_id, is_root=False)
    db.add(team)
    await db.flush()
    await db.refresh(team)
    if settings.files_enabled:
        # In the same transaction as the team row: a team that exists but has
        # no folder is a state the drive would have to reconcile later.
        ctx = ActingContext.for_service(
            token_id=team.id,
            org_id=root_id,
            label="team_service",
            credential=CredentialKind.CI_TOKEN,
        )
        async with files_transaction(db, ctx) as repo:
            await drives.ensure_team_folder(repo, ctx, team.id, team_name=team.name)
    return team


async def rename(db: AsyncSession, team: Team, name: str) -> Team:
    team.name = name
    await db.flush()
    return team


async def delete_team(db: AsyncSession, team: Team) -> None:
    """Delete an EMPTY non-root team.

    Strict: refuses a root team, a team with any sub-team, or a team that
    still has members — the admin must clear them first. Without these
    guards, a team with sub-teams would surface the FK `RESTRICT` violation
    as a raw 500 instead of a clean 400.

    Also refuses when the team still owns money (a funded billing pool or any
    credit-ledger history) or anything a domain registered as blocking (a
    stored connection credential): unlike the tree FKs those are `ON DELETE
    CASCADE`, so the delete would destroy purchased prepaid credit, the
    append-only ledger, and encrypted warehouse secrets with no trace and no
    undo.
    """
    if team.is_root:
        raise TeamConflictError("Cannot delete a root team via this path")
    org_id = await org_root_id(db, team.id)
    await lock_org_tree(db, org_id)
    if await descendant_ids(db, team.id):
        raise TeamConflictError("Cannot delete a team that has sub-teams; delete them first")
    has_member = (
        await db.execute(
            select(TeamMembership.id).where(TeamMembership.team_id == team.id).limit(1)
        )
    ).first()
    if has_member is not None:
        raise TeamConflictError("Cannot delete a team that still has members; remove them first")
    if await org_entitlements().team_holds_credit(db, team.id):
        raise TeamConflictError(
            "Cannot delete a team that owns credit or ledger history; "
            "move its balance to another pool first"
        )
    refusal = await removal_hooks.team_delete_refusal(db, team_id=team.id)
    if refusal is not None:
        raise TeamConflictError(refusal)
    # What the team held passes to the team above it.
    await removal_hooks.team_deleted(
        db, org_id=org_id, team_id=team.id, parent_team_id=team.parent_team_id or org_id
    )
    await db.delete(team)
    await db.flush()


async def reparent_team(db: AsyncSession, *, team: Team, new_parent_id: UUID) -> Team:
    """Move `team` (and its whole subtree) under `new_parent_id`, then repair
    the materialized ancestor memberships so the chain rule still holds.

    Caller enforces permissions and that both teams are in the same org.
    Raises `TeamConflictError` on a root move, a self/descendant target
    (cycle), or a missing parent.

    Membership repair (the delicate part):
      * ADD — every user in the moved subtree gains a MEMBER row in each NEW
        ancestor they didn't already sit under, so "in a leaf ⇒ in every
        ancestor" never breaks (no access silently lost).
      * REMOVE — for each ancestor the subtree no longer sits under, a
        subtree user's now-orphaned MEMBER row is dropped UNLESS they remain
        in that ancestor's subtree through another branch. ADMIN rows are
        never auto-stripped. The one irreducibly-ambiguous case — a user who
        is a *direct* MEMBER of an intermediate old-ancestor that also held
        the moved subtree, with no sibling branch — loses that row; the
        membership schema can't tell a direct row from an inherited one
        (an `is_direct` flag would be the precise fix). This is pinned by an
        explicit test so the behavior is a conscious contract.
    """
    if team.is_root:
        raise TeamConflictError("Cannot move the organization root")
    if new_parent_id == team.id:
        raise TeamConflictError("A team cannot be its own parent")
    # Every read below feeds the cycle guard, so the whole check-then-write runs
    # under the org's tree lock: two simultaneous moves that would each be
    # rejected in sequence must not both see a pre-move tree and both commit.
    org_id = await org_root_id(db, team.id)
    await lock_org_tree(db, org_id)
    subtree_ids = {team.id, *(await descendant_ids(db, team.id))}
    if new_parent_id in subtree_ids:
        raise TeamConflictError("Cannot move a team under one of its own descendants")
    new_parent = await get_by_id(db, new_parent_id)
    if new_parent is None:
        raise TeamConflictError("New parent team does not exist")
    if team.parent_team_id == new_parent_id:
        return team  # no-op

    # Keep the OLD ancestors in leaf-first order: the REMOVE pass below must
    # delete deeper ancestors before shallower ones, so a parent's justification
    # check sees its already-shed child's row gone. Iterating a set difference
    # instead would make the outcome depend on hash order.
    old_chain_ids: list[UUID] = []
    if team.parent_team_id is not None:
        old_chain_ids = [t.id for t in await ancestor_chain(db, team.parent_team_id)]
    old_anc_ids = set(old_chain_ids)
    new_anc_ids = {t.id for t in await ancestor_chain(db, new_parent_id)}

    subtree_users = set(
        (
            await db.execute(
                select(TeamMembership.user_id)
                .where(
                    TeamMembership.org_team_id == org_id,
                    TeamMembership.team_id.in_(subtree_ids),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    team.parent_team_id = new_parent_id
    await db.flush()

    # Fail closed: re-derive the chain from the written row and refuse (rolling
    # the transaction back) unless it still terminates at a root. A cycle here
    # would be unrepairable through the API — every route 404s on a team whose
    # chain never reaches the org root.
    written_chain = await ancestor_chain(db, team.id)
    if not written_chain or not written_chain[-1].is_root:
        raise TeamConflictError("Move would leave the team outside its organization tree")

    if not subtree_users:
        await db.refresh(team)
        return team

    # ADD: materialize the subtree's users into each newly-acquired ancestor.
    for ancestor_id in new_anc_ids - old_anc_ids:
        existing = set(
            (
                await db.execute(
                    select(TeamMembership.user_id).where(
                        TeamMembership.org_team_id == org_id,
                        TeamMembership.team_id == ancestor_id,
                        TeamMembership.user_id.in_(subtree_users),
                    )
                )
            )
            .scalars()
            .all()
        )
        for user_id in subtree_users - existing:
            db.add(TeamMembership(user_id=user_id, team_id=ancestor_id, role=TeamRole.MEMBER))

    # REMOVE: drop now-unjustified MEMBER rows in each shed ancestor, leaf-first.
    # After the flush above, `descendant_ids(ancestor)` already excludes the moved
    # subtree, so a surviving membership there is the "other branch" justification;
    # processing deeper-first means a just-deleted child row is already gone when
    # its parent is evaluated.
    for ancestor_id in old_chain_ids:
        if ancestor_id in new_anc_ids:
            continue
        remaining = await descendant_ids(db, ancestor_id)
        for user_id in subtree_users:
            membership = (
                await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.org_team_id == org_id,
                        TeamMembership.team_id == ancestor_id,
                        TeamMembership.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None or membership.role is TeamRole.ADMIN:
                continue
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
                await db.delete(membership)
                # Flush so a parent ancestor's justification query (next outer
                # iteration) sees this child row already gone — the session has
                # autoflush disabled, so without this the delete stays invisible.
                await db.flush()

    await db.refresh(team)
    return team


async def create_org_rows(db: AsyncSession, *, org_name: str) -> Team:
    """The org's own rows (the root team and its default settings) and the
    rows each registered domain starts an org with."""
    org = Team(name=org_name, is_root=True, parent_team_id=None)
    db.add(org)
    await db.flush()  # org.id

    # Every org gets an explicit settings row (defaults: all providers allowed).
    db.add(OrgSettings(org_team_id=org.id))
    # And whatever per-org rows the installed domains start an org with.
    await creation_hooks.org_created(db, org_id=org.id)
    return org


async def seat_founder(
    db: AsyncSession, *, org: Team, founder_id: UUID, created_at: datetime | None = None
) -> None:
    """The founder's standing in the org they created: Admin of the root, the
    OWNER role assignment, and the creation counted against their cap. Needs
    the founder's org membership to exist already (the root seat's foreign key
    names it)."""
    db.add(TeamMembership(user_id=founder_id, team_id=org.id, role=TeamRole.ADMIN))
    # The founding admin is the org's owner: the one role assignment every org
    # carries from birth (the migration that added the table back-filled it for
    # the orgs that predate it). Same transaction as the root and the membership,
    # so an org can never exist without its owner row.
    db.add(
        RoleAssignment(
            org_team_id=org.id,
            principal_kind=PrincipalKind.USER,
            principal_id=founder_id,
            scope_kind=ScopeKind.ORG,
            scope_id=org.id,
            role=Role.OWNER,
            granted_by_id=None,
        )
    )
    # Counted toward the founder's per-identity org-creation cap
    # (``backend.services.abuse.org_creation.org_creation_allowed``).
    creation = IdentityOrgCreation(user_id=founder_id, org_team_id=org.id)
    if created_at is not None:
        creation.created_at = created_at
    db.add(creation)
    await db.flush()


async def create_org_with_admin(
    db: AsyncSession,
    *,
    org_name: str,
    admin_email: str,
    admin_first_name: str,
    admin_last_name: str,
    admin_password: str | None,
    admin_platform_role: PlatformRole | None = None,
) -> tuple[Team, User]:
    """Single-transaction bootstrap: root team + admin user + Admin membership
    of the root + default org settings. Used by `/admin/v1/orgs`, the dev seed,
    and the OAuth/email signup paths.

    `admin_password` is None for OAuth-initiated signups (no local credential).
    The new user's org membership is written by the database with the user row.
    """
    org = await create_org_rows(db, org_name=org_name)
    admin = await user_service.create_user(
        db,
        org_team_id=org.id,
        email=admin_email,
        first_name=admin_first_name,
        last_name=admin_last_name,
        password=admin_password,
        platform_role=admin_platform_role,
    )
    await seat_founder(db, org=org, founder_id=admin.id)
    return org, admin


async def delete_org(db: AsyncSession, org: Team) -> None:
    """Delete a root team and everything beneath it, and never a person who
    belongs elsewhere.

    1. Every credential anyone holds in the org is ended first
       (``revoke_memberships``), so nothing minted for the org outlives a row
       it names.
    2. Rows hanging off a team (memberships, allocations, billing pools and
       their grants, machine credentials, personal access, CI and proxy tokens,
       Slack installs) go with it through their ``ON DELETE CASCADE`` foreign
       keys.
    3. ``users.org_team_id`` is ``RESTRICT``, so every identity whose HOME is
       this org is dealt with before the org goes: one that holds another
       membership keeps its account and is re-homed to that org (an active
       membership first, the oldest first); one with no other membership is
       deleted, as before. An identity with no org at all is not
       representable until the home column may be empty.
    4. ``teams.parent_team_id`` is ``RESTRICT`` too: the tree is deleted
       leaf-first, one team at a time, so no statement ever removes a team
       that still has a child and no team is ever rewritten on its way out.
    """
    if not org.is_root:
        raise TeamConflictError("Only a root (org) team can be deleted this way")
    # Serialize with anything that grows the tree, so a sub-team created
    # mid-delete cannot strand a child under a parent that is going away.
    await lock_org_tree(db, org.id)
    memberships = list(
        (await db.execute(select(OrgMembership).where(OrgMembership.org_team_id == org.id)))
        .scalars()
        .all()
    )
    await revoke_memberships(db, memberships, reason="org_deleted")
    await removal_hooks.org_deleted(db, org_id=org.id)
    homed = (await db.execute(users_homed_in(org.id))).scalars().all()
    for person in homed:
        elsewhere = (
            await db.execute(
                select(OrgMembership)
                .where(
                    OrgMembership.user_id == person.id,
                    OrgMembership.org_team_id != org.id,
                )
                .order_by(
                    (OrgMembership.status == MembershipStatus.ACTIVE).desc(),
                    OrgMembership.joined_at,
                    OrgMembership.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if elsewhere is not None:
            await repoint_home(db, person, elsewhere.org_team_id)
            log.info(
                "org.delete.identity_rehomed",
                org_id=str(org.id),
                user_id=str(person.id),
                home_org_id=str(elsewhere.org_team_id),
            )
        else:
            await db.delete(person)
            log.info("org.delete.identity_deleted", org_id=str(org.id), user_id=str(person.id))
    await db.flush()
    # Breadth-first, so reversed it is deepest-first: every team goes before
    # its parent.
    for team_id in reversed(await descendant_ids(db, org.id)):
        await db.execute(
            delete(Team).where(Team.id == team_id).execution_options(synchronize_session=False)
        )
    await db.delete(org)
    await db.flush()


@asynccontextmanager
async def files_transaction(
    db: AsyncSession, ctx: ActingContext, *, on_domain_created: drives.DomainCreated | None = None
) -> AsyncIterator[FilesRepo]:
    """A Files repo joined to the caller's transaction, org drive ensured.

    It lives here because all three bridge call sites — objects, membership and
    teams — already sit above ``team_service`` in the import order.

    Making a brand-new dedup domain marks it in the object store, and by
    default that write happens here, inside the caller's transaction. A caller
    that holds a lock across this block must pass its own
    ``on_domain_created`` — one that records the domain and writes the marker
    after it has committed — so nothing waits on the store behind a lock.
    """
    from backend.services.files.context import ensure_store_row, stamp_new_domain

    repo = FilesRepo.joined(db, OrgScope(org_team_id=ctx.org_id))
    store = await ensure_store_row(db, settings)
    await db.flush()
    async with repo.transaction():
        await drives.ensure_org_drive(
            repo,
            ctx,
            ctx.org_id,
            store_id=store.id,
            on_domain_created=on_domain_created or stamp_new_domain(store.id, settings, db),
        )
        yield repo


#: The names the ``backend.services.org`` package exports for these.
org_root_of = org_root_id
teams_administered_by = admin_team_ids
