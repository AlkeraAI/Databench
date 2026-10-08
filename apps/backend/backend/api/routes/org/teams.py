"""Teams routes — scoped to the caller's organization."""

from __future__ import annotations

from uuid import UUID

from alkera_core.models import Team
from alkera_core.schemas.tenancy.team import TeamCreate, TeamRead
from alkera_core.schemas.tenancy.team_membership import TeamMemberRead
from alkera_core.validation.display_name import OptionalDisplayNameStr
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import (
    CurrentOrg,
    CurrentUser,
    DbSession,
    OrgAdmin,
    require_email_verified,
    require_team_admin,
)
from backend.services.org import RosterEntry, TeamConflictError
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


def member_read(entry: RosterEntry) -> TeamMemberRead:
    """The wire shape of one roster entry: the direct row, the descent that
    reaches the team, and the standing the two make."""
    descent = entry.descent
    return TeamMemberRead(
        user_id=entry.user.id,
        display_name=entry.user.display_name,
        email=entry.user.email,
        first_name=entry.user.first_name,
        last_name=entry.user.last_name,
        role=entry.effective_role,
        team_id=entry.team.id,
        team_name=entry.team.name,
        created_at=entry.created_at,
        direct_role=entry.direct_role,
        descent_role=descent.role if descent is not None else None,
        descent_from_team_id=descent.from_team.id if descent is not None else None,
        descent_from_team_name=descent.from_team.name if descent is not None else None,
    )


class TeamUpdate(BaseModel):
    # OptionalDisplayNameStr: a rename lands in the same invitation-email prose a
    # create does, so it is held to the same policy (a team renamed AFTER its
    # members were invited still names itself in every later invite).
    name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)


class TeamMove(BaseModel):
    new_parent_team_id: UUID


def _team_read(team: Team, member_count: int) -> TeamRead:
    """TeamRead with the computed (non-column) member_count attached."""
    return TeamRead.model_validate(team).model_copy(update={"member_count": member_count})


async def _ensure_in_org(db: AsyncSession, team: Team | None, org_id: UUID) -> None:
    if team is None or not await _belongs_to_org(db, team, org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


async def _belongs_to_org(db: AsyncSession, team: Team, org_team_id: UUID) -> bool:
    """True iff `team` is in `org_team_id`'s tree. Delegates to the shared
    full-chain check so the teams + memberships routers agree exactly."""
    return await team_service.belongs_to_org(db, team.id, org_team_id)


@router.get("", response_model=list[TeamRead])
async def list_teams(caller: CurrentUser, db: DbSession, org_id: CurrentOrg) -> list[TeamRead]:
    rows = await team_service.list_in_org(db, org_id)
    counts = await team_service.member_counts(db, [t.id for t in rows])
    return [_team_read(t, counts.get(t.id, 0)) for t in rows]


@router.post(
    "",
    response_model=TeamRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_email_verified)],
)
async def create_team(
    payload: TeamCreate, db: DbSession, caller: OrgAdmin, org_id: CurrentOrg
) -> TeamRead:
    parent_id = payload.parent_team_id or org_id
    if parent_id != org_id:
        # Validate parent is in caller's org tree.
        parent = await team_service.get_by_id(db, parent_id)
        await _ensure_in_org(db, parent, org_id)
    try:
        team = await team_service.create_subteam(
            db,
            org_team_id=org_id,
            name=payload.name,
            parent_team_id=parent_id,
        )
    except TeamConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _team_read(team, 0)


@router.get("/{team_id}", response_model=TeamRead)
async def get_team(
    team_id: UUID, caller: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> TeamRead:
    team = await team_service.get_by_id(db, team_id)
    await _ensure_in_org(db, team, org_id)
    assert team is not None
    counts = await team_service.member_counts(db, [team.id])
    return _team_read(team, counts.get(team.id, 0))


@router.get(
    "/{team_id}/members",
    response_model=list[TeamMemberRead],
    dependencies=[Depends(require_team_admin())],
)
async def list_team_members(
    team_id: UUID,
    caller: CurrentUser,
    db: DbSession,
    org_id: CurrentOrg,
    include_descendants: bool = False,
) -> list[TeamMemberRead]:
    """Everyone who stands on the team: each person with a row on it (their
    direct standing) and each person an admin row above reaches by descent —
    one row per person per team, with the direct role, the descent and the
    standing the two make (see ``TeamMemberRead``). With
    `include_descendants`, the same for every team in the subtree, one team
    after another. Team-admin only (it exposes member emails)."""
    team = await team_service.get_by_id(db, team_id)
    await _ensure_in_org(db, team, org_id)
    assert team is not None
    team_ids = [team_id]
    if include_descendants:
        team_ids += await team_service.descendant_ids(db, team_id)
    out: list[TeamMemberRead] = []
    for tid in team_ids:
        out.extend(member_read(e) for e in await membership_service.roster(db, team_id=tid))
    return out


@router.patch(
    "/{team_id}",
    response_model=TeamRead,
    dependencies=[Depends(require_team_admin()), Depends(require_email_verified)],
)
async def update_team(
    team_id: UUID, payload: TeamUpdate, caller: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> TeamRead:
    team = await team_service.get_by_id(db, team_id)
    await _ensure_in_org(db, team, org_id)
    assert team is not None  # narrowed by _ensure_in_org's raise paths
    if payload.name is not None:
        team = await team_service.rename(db, team, payload.name)
    counts = await team_service.member_counts(db, [team.id])
    return _team_read(team, counts.get(team.id, 0))


@router.post(
    "/{team_id}/move",
    response_model=TeamRead,
    dependencies=[Depends(require_email_verified)],
)
async def move_team(
    team_id: UUID, payload: TeamMove, db: DbSession, caller: OrgAdmin, org_id: CurrentOrg
) -> TeamRead:
    """Re-parent a team (and its subtree) under another team in the same org.
    Org-admin only — re-parenting is a structural change like create/delete."""
    team = await team_service.get_by_id(db, team_id)
    await _ensure_in_org(db, team, org_id)
    assert team is not None
    new_parent = await team_service.get_by_id(db, payload.new_parent_team_id)
    await _ensure_in_org(db, new_parent, org_id)
    try:
        moved = await team_service.reparent_team(
            db, team=team, new_parent_id=payload.new_parent_team_id
        )
    except TeamConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    counts = await team_service.member_counts(db, [moved.id])
    return _team_read(moved, counts.get(moved.id, 0))


@router.delete(
    "/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_email_verified)],
)
async def delete_team(team_id: UUID, db: DbSession, caller: OrgAdmin, org_id: CurrentOrg) -> None:
    team = await team_service.get_by_id(db, team_id)
    await _ensure_in_org(db, team, org_id)
    assert team is not None
    try:
        await team_service.delete_team(db, team)
    except TeamConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IntegrityError as exc:
        # Defensive: the service guards children/members, so an FK RESTRICT
        # should never reach here — but never let it surface as a 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a team that still has sub-teams or members",
        ) from exc
