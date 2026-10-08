"""Storage ceilings: what an org may store, and what each member may own.

The org's *effective* ceiling is decided in one place: the platform admin's
override when one is set, else what the org's plan includes, else the figure
the drive was born with (the deployment's configured default). A member's own
ceilings are the rows an org admin (org-wide) or a team admin (inside that
team's folder) wrote for them.

Everything here runs as the application, *outside* the Files transaction: the
Files role can read the tree but not ``teams`` or the billing tables, so the
ceilings are resolved before a write opens its transaction and handed to the
quota service as plain data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from alkera_core.allocation_tree import effective_terms, summed_limit
from alkera_core.authz.principal import ActingContext
from alkera_core.cap_versions import cap_version
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_or_insert
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import drives
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DriveId, OrgScope
from alkera_core.files.quota import Ceilings, CeilingsResolver, QuotaService, UserCeiling
from alkera_core.files.repo import FilesRepo, named_folder_paths
from alkera_core.models import OrgStorageLimit, Team, TeamMembership, UserStorageLimit
from alkera_core.models.allocations import AllocationResource, TeamAllocation
from alkera_core.models.files.stores import FileDrive
from alkera_core.org_entitlements import org_entitlements
from sqlalchemy import false, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

OrgLimitSource = Literal["override", "plan", "default"]
MemberLimitSource = Literal["user", "team", "org", "plan"]


@dataclass(frozen=True, slots=True)
class EffectiveOrgLimit:
    limit_bytes: int | None
    source: OrgLimitSource
    plan: str


@dataclass(frozen=True, slots=True)
class MemberStorage:
    """One member's usage next to the tightest ceiling that binds them.

    ``limit_team_id`` / ``limit_team_name`` name the team whose allowance or
    cap the binding figure is, when a team's is; ``None`` when the org's or the
    member's org-wide cap binds."""

    used_bytes: int
    limit_bytes: int | None
    limit_source: MemberLimitSource | None
    over_limit: bool
    org_used_bytes: int
    org_limit_bytes: int | None
    limit_team_id: UUID | None = None
    limit_team_name: str | None = None


@dataclass(frozen=True, slots=True)
class SummedStorage:
    """A member's storage allowance summed over their leaf teams, and the team
    to name for it: the one whose allowance bound the single term, or whose
    cap it is; ``None`` when nothing bounds them or several terms add up."""

    limit_bytes: int | None
    team_id: UUID | None


# ---- the org's plan and override -----------------------------------------


async def org_override(db: AsyncSession, org_id: UUID) -> OrgStorageLimit | None:
    return await db.get(OrgStorageLimit, org_id)


def drive_default_bytes(drive: FileDrive | None) -> int:
    """The figure the org's drive holds when neither an override nor the plan
    sets one: the drive's own, or — before its first Files request creates it —
    the deployment default it will be born with. Never zero for a missing drive:
    that would read as "may store nothing" on an org that simply hasn't stored
    anything yet."""
    return settings.files_quota_default_bytes if drive is None else drive.quota_bytes


async def effective_org_limit(
    db: AsyncSession, org_id: UUID, *, fallback_bytes: int
) -> EffectiveOrgLimit:
    """Override, else plan, else the drive's own figure."""
    entitlements = org_entitlements()
    plan = await entitlements.plan(db, org_id)
    row = await org_override(db, org_id)
    if row is not None:
        return EffectiveOrgLimit(limit_bytes=row.limit_bytes, source="override", plan=plan)
    plan_bytes = await entitlements.plan_storage_bytes(db, org_id)
    if plan_bytes is not None:
        return EffectiveOrgLimit(limit_bytes=plan_bytes, source="plan", plan=plan)
    return EffectiveOrgLimit(limit_bytes=fallback_bytes, source="default", plan=plan)


async def set_org_override(
    db: AsyncSession, org_id: UUID, *, limit_bytes: int | None, by: UUID | None
) -> OrgStorageLimit:
    locked, _created = await lock_or_insert(
        db,
        LockRank.ORG_SETTINGS,
        select(OrgStorageLimit)
        .where(OrgStorageLimit.org_team_id == org_id)
        .execution_options(populate_existing=True),
        insert(OrgStorageLimit).values(
            org_team_id=org_id, limit_bytes=limit_bytes, created_by_id=by
        ),
    )
    row = locked.scalar_one()
    row.limit_bytes = limit_bytes
    row.created_by_id = by
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


async def clear_org_override(db: AsyncSession, org_id: UUID) -> bool:
    row = await org_override(db, org_id)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


# ---- a member's own limits -----------------------------------------------


async def user_limits_for(
    db: AsyncSession, *, org_id: UUID, user_id: UUID
) -> list[UserStorageLimit]:
    rows = (
        await db.execute(
            select(UserStorageLimit)
            .where(UserStorageLimit.org_team_id == org_id, UserStorageLimit.user_id == user_id)
            .order_by(UserStorageLimit.team_id.nulls_first())
        )
    ).scalars()
    return list(rows)


def user_limit_version(row: UserStorageLimit | None) -> str:
    """The version tag of a member's storage cap slot — of the row, or of its absence."""
    return cap_version(
        None if row is None else row.limit_bytes, None if row is None else row.updated_at
    )


async def set_user_limit(
    db: AsyncSession,
    *,
    org_id: UUID,
    team_id: UUID | None,
    user_id: UUID,
    limit_bytes: int,
    by: UUID | None,
) -> UserStorageLimit:
    locked, _created = await lock_or_insert(
        db,
        LockRank.ORG_SETTINGS,
        select(UserStorageLimit)
        .where(
            UserStorageLimit.org_team_id == org_id,
            UserStorageLimit.user_id == user_id,
            UserStorageLimit.team_id.is_(None)
            if team_id is None
            else UserStorageLimit.team_id == team_id,
        )
        .execution_options(populate_existing=True),
        insert(UserStorageLimit).values(
            org_team_id=org_id,
            team_id=team_id,
            user_id=user_id,
            limit_bytes=limit_bytes,
            created_by_id=by,
            updated_at=datetime.now(UTC),
        ),
    )
    row = locked.scalar_one()
    row.limit_bytes = limit_bytes
    row.created_by_id = by
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


async def clear_user_limit(
    db: AsyncSession, *, org_id: UUID, team_id: UUID | None, user_id: UUID
) -> bool:
    row = (
        await db.execute(
            select(UserStorageLimit).where(
                UserStorageLimit.org_team_id == org_id,
                UserStorageLimit.user_id == user_id,
                UserStorageLimit.team_id.is_(None)
                if team_id is None
                else UserStorageLimit.team_id == team_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


# ---- usage -----------------------------------------------------------------


async def org_drive(db: AsyncSession, org_id: UUID) -> FileDrive | None:
    """The org's drive row, or ``None`` before its first Files request."""
    repo = FilesRepo(db, OrgScope(org_team_id=org_id))
    async with repo.transaction():
        return await repo.drive_for_org()


async def drive_used_bytes(db: AsyncSession, ctx: ActingContext, drive: FileDrive) -> int:
    """The drive's committed bytes, read the way the quota check reads them."""
    repo = FilesRepo(db, OrgScope(org_team_id=drive.org_team_id))
    async with repo.transaction():
        usage = await QuotaService(repo, ctx, SystemClock()).usage(DriveId(drive.id))
    return usage.bytes


async def user_used_bytes(
    db: AsyncSession, *, drive: FileDrive, user_id: UUID, scope_path: str | None = None
) -> int:
    """The live files charged to ``user_id`` in the drive — the ones they
    created and everything in their chats, whoever wrote it — summed,
    optionally only under the folder at ``scope_path``. The same aggregate
    the member-limit check reads, so the card shows what is enforced."""
    repo = FilesRepo(db, OrgScope(org_team_id=drive.org_team_id))
    async with repo.transaction():
        if scope_path is None:
            return await repo.owned_bytes(DriveId(drive.id), user_id)
        return await repo.owned_bytes_under(DriveId(drive.id), user_id, scope_path)


async def team_names(db: AsyncSession, team_ids: Iterable[UUID]) -> dict[UUID, str]:
    """The names of ``team_ids``, for the ceilings and refusals that name a team."""
    ids = list(team_ids)
    if not ids:
        return {}
    return {
        team_id: name
        for team_id, name in (
            await db.execute(select(Team.id, Team.name).where(Team.id.in_(ids)))
        ).all()
    }


async def team_folder_paths(
    db: AsyncSession,
    *,
    drive: FileDrive,
    team_ids: list[UUID],
    names: dict[UUID, str] | None = None,
) -> dict[UUID, str]:
    """``path_ids`` of ``/Teams/<team>`` for each team that has a folder.

    A team without a folder yet has nothing under it, so a limit scoped to it
    binds no write until the folder exists. Two reads: the teams' names (unless
    the caller already has them), then their folders in one statement.
    """
    if not team_ids or drive.root_node_id is None:
        return {}
    if names is None:
        names = await team_names(db, team_ids)
    by_name = {
        name.encode("utf-8"): team_id for team_id, name in names.items() if team_id in team_ids
    }
    found = await named_folder_paths(
        db,
        org_team_id=drive.org_team_id,
        root_node_id=drive.root_node_id,
        container_name=drives.TEAMS_NAME,
        names=by_name,
    )
    return {by_name[name]: path for name, path in found.items()}


@dataclass(frozen=True, slots=True)
class _LimitFacts:
    """Everything the ceilings need about one member, from one statement."""

    override_set: bool
    override_bytes: int | None
    rows: list[tuple[UUID | None, int]]


async def _limit_facts(db: AsyncSession, *, org_id: UUID, user_id: UUID | None) -> _LimitFacts:
    """One round trip: the override row and the member's own limit rows. ``user_id`` is ``None``
    for a caller that is nobody's member — a box on its machine credential —
    whose writes meet the org's ceiling and no person's rows."""
    override = (
        select(OrgStorageLimit.limit_bytes, literal(True))
        .where(OrgStorageLimit.org_team_id == org_id)
        .subquery()
    )
    rows = (
        select(
            func.json_agg(
                func.json_build_object(
                    "team_id",
                    UserStorageLimit.team_id,
                    "limit_bytes",
                    UserStorageLimit.limit_bytes,
                )
            )
        )
        .where(
            UserStorageLimit.org_team_id == org_id,
            UserStorageLimit.user_id == user_id if user_id is not None else false(),
        )
        .scalar_subquery()
    )
    statement = select(
        select(override.c[1]).scalar_subquery(),
        select(override.c[0]).scalar_subquery(),
        rows,
    )
    row = (await db.execute(statement)).one()
    raw = row[2]
    parsed = raw if isinstance(raw, list) else json.loads(raw or "[]")
    return _LimitFacts(
        override_set=bool(row[0]),
        override_bytes=row[1],
        rows=[
            (
                None if item["team_id"] is None else UUID(str(item["team_id"])),
                int(item["limit_bytes"]),
            )
            for item in parsed
        ],
    )


async def ceilings_for(
    db: AsyncSession, *, org_id: UUID, user_id: UUID | None, drive: FileDrive
) -> Ceilings:
    """Everything the quota check needs about this caller on this drive. With
    no ``user_id`` — a box on its machine credential — the org's ceiling is the
    whole answer."""
    facts = await _limit_facts(db, org_id=org_id, user_id=user_id)
    if facts.override_set:
        org_bytes = facts.override_bytes
    else:
        plan = await org_entitlements().plan_storage_bytes(db, org_id)
        org_bytes = drive.quota_bytes if plan is None else plan
    team_ids = [team_id for team_id, _ in facts.rows if team_id is not None]
    summed = (
        SummedStorage(limit_bytes=None, team_id=None)
        if user_id is None
        else await summed_team_limit(db, org_id=org_id, user_id=user_id, rows=facts.rows)
    )
    names = await team_names(
        db, [*team_ids, *([summed.team_id] if summed.team_id is not None else [])]
    )
    paths = await team_folder_paths(db, drive=drive, team_ids=team_ids, names=names)
    users: list[UserCeiling] = []
    for team_id, limit_bytes in facts.rows:
        if team_id is None:
            users.append(UserCeiling(limit_bytes=limit_bytes, scope="org"))
        elif team_id in paths:
            users.append(
                UserCeiling(
                    limit_bytes=limit_bytes,
                    scope="team",
                    team_id=team_id,
                    scope_path=paths[team_id],
                    team_name=names.get(team_id),
                )
            )
    if summed.limit_bytes is not None:
        users.append(
            UserCeiling(
                limit_bytes=summed.limit_bytes,
                scope="org",
                team_id=summed.team_id,
                team_name=None if summed.team_id is None else names.get(summed.team_id),
            )
        )
    return Ceilings(org_bytes=org_bytes, users=tuple(users))


async def summed_team_limit(
    db: AsyncSession, *, org_id: UUID, user_id: UUID, rows: list[tuple[UUID | None, int]]
) -> SummedStorage:
    """The member's storage allowance summed over their leaf teams — per team,
    their own limit in it held to the tightest allowance on the team's chain,
    else that allowance, else no limit (which makes the sum unlimited). It binds
    the bytes they own anywhere in the drive, beside any per-folder limit."""
    member_of = set(
        (
            await db.execute(
                select(TeamMembership.team_id).where(
                    TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    if not member_of:
        return SummedStorage(limit_bytes=None, team_id=None)
    parent_of = {
        team_id: parent
        for team_id, parent in (
            await db.execute(select(Team.id, Team.parent_team_id).where(Team.id.in_(member_of)))
        ).all()
    }
    allocation = {
        team_id: int(limit)
        for team_id, limit in (
            await db.execute(
                select(TeamAllocation.team_id, TeamAllocation.limit_bytes).where(
                    TeamAllocation.org_team_id == org_id,
                    TeamAllocation.team_id.in_(member_of),
                    TeamAllocation.resource == AllocationResource.STORAGE.value,
                    TeamAllocation.limit_bytes.is_not(None),
                )
            )
        ).all()
    }
    per_user = {team_id: limit for team_id, limit in rows if team_id is not None}
    terms = effective_terms(
        member_of, parent_of=parent_of, per_user=per_user, team_allocation=allocation
    )
    limit = summed_limit(term.limit for term in terms)
    named: UUID | None = None
    if limit is not None and len(terms) == 1:
        # One placed team: name the team whose allowance bound it, else the
        # team the member's own cap sits on.
        named = terms[0].bound_by if terms[0].bound_by is not None else terms[0].team_id
    return SummedStorage(limit_bytes=limit, team_id=named)


def ceilings_resolver(*, org_id: UUID, user_id: UUID | None, drive: FileDrive) -> CeilingsResolver:
    """A resolver a write consults lazily, on a session of its own.

    The Files transaction runs as the Files role, which cannot read ``teams``
    or the billing tables, so the facts are read as the application on a
    fresh session when a write first asks — and never for a request that
    never asks (a rename, a read).
    """

    async def resolve() -> Ceilings:
        async with AsyncSessionLocal() as session:
            return await ceilings_for(session, org_id=org_id, user_id=user_id, drive=drive)

    return resolve


@dataclass(frozen=True, slots=True)
class OrgStorageFacts:
    """The org half of every member's picture, read once for a whole roster."""

    drive: FileDrive | None
    limit: EffectiveOrgLimit
    used_bytes: int


async def org_storage_facts(
    db: AsyncSession, ctx: ActingContext, *, org_id: UUID
) -> OrgStorageFacts:
    drive = await org_drive(db, org_id)
    org = await effective_org_limit(db, org_id, fallback_bytes=drive_default_bytes(drive))
    used = 0 if drive is None else await drive_used_bytes(db, ctx, drive)
    return OrgStorageFacts(drive=drive, limit=org, used_bytes=used)


async def member_storage(
    db: AsyncSession, ctx: ActingContext, *, org_id: UUID, user_id: UUID
) -> MemberStorage:
    """The member's own picture: usage, the tightest ceiling, safety mode."""
    facts = await org_storage_facts(db, ctx, org_id=org_id)
    return await member_picture(db, org=facts, org_id=org_id, user_id=user_id)


async def member_picture(
    db: AsyncSession, *, org: OrgStorageFacts, org_id: UUID, user_id: UUID
) -> MemberStorage:
    """One member's picture against org facts already read — what the member's
    own reading shows, and what an admin's roster shows for them, from one
    computation so the two can never disagree."""
    drive = org.drive
    used = 0 if drive is None else await user_used_bytes(db, drive=drive, user_id=user_id)
    rows = await user_limits_for(db, org_id=org_id, user_id=user_id)
    summed = await summed_team_limit(
        db, org_id=org_id, user_id=user_id, rows=[(r.team_id, r.limit_bytes) for r in rows]
    )
    team_ids = [r.team_id for r in rows if r.team_id is not None]
    names = await team_names(
        db, [*team_ids, *([summed.team_id] if summed.team_id is not None else [])]
    )
    paths = (
        {}
        if drive is None
        else await team_folder_paths(db, drive=drive, team_ids=team_ids, names=names)
    )

    # Every (ceiling, usage-against-it, source, team) that binds this member;
    # the tightest by remaining room is the one the card shows.
    candidates: list[tuple[int, int, MemberLimitSource, UUID | None]] = []
    if org.limit.limit_bytes is not None:
        candidates.append(
            (org.limit.limit_bytes, org.used_bytes, _org_source(org.limit.source), None)
        )
    for row in rows:
        if row.team_id is None:
            candidates.append((row.limit_bytes, used, "user", None))
        elif row.team_id in paths and drive is not None:
            under = await user_used_bytes(
                db, drive=drive, user_id=user_id, scope_path=paths[row.team_id]
            )
            candidates.append((row.limit_bytes, under, "team", row.team_id))
    if summed.limit_bytes is not None:
        candidates.append((summed.limit_bytes, used, "team", summed.team_id))
    over = any(spent >= limit for limit, spent, _, _ in candidates)
    if not candidates:
        return MemberStorage(
            used_bytes=used,
            limit_bytes=None,
            limit_source=None,
            over_limit=False,
            org_used_bytes=org.used_bytes,
            org_limit_bytes=None,
        )
    limit, _spent, source, team_id = min(candidates, key=lambda c: c[0] - c[1])
    return MemberStorage(
        used_bytes=used,
        limit_bytes=limit,
        limit_source=source,
        over_limit=over,
        org_used_bytes=org.used_bytes,
        org_limit_bytes=org.limit.limit_bytes,
        limit_team_id=team_id,
        limit_team_name=None if team_id is None else names.get(team_id),
    )


def _org_source(source: OrgLimitSource) -> MemberLimitSource:
    return "plan" if source == "plan" else "org"


__all__ = [
    "EffectiveOrgLimit",
    "MemberStorage",
    "OrgStorageFacts",
    "SummedStorage",
    "ceilings_for",
    "ceilings_resolver",
    "clear_org_override",
    "clear_user_limit",
    "drive_default_bytes",
    "drive_used_bytes",
    "effective_org_limit",
    "member_picture",
    "member_storage",
    "org_drive",
    "org_override",
    "org_storage_facts",
    "set_org_override",
    "set_user_limit",
    "summed_team_limit",
    "team_folder_paths",
    "team_names",
    "user_limit_version",
    "user_limits_for",
    "user_used_bytes",
]
