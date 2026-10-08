"""Team membership routes — nested under /teams/{team_id}/memberships."""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.models import TeamRole, User
from alkera_core.observability.asgi import names_body_errors
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.tenancy.team_membership import (
    TeamMembershipCreate,
    TeamMembershipRead,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    _is_admin_of_team_or_ancestor,
    require_email_verified,
    require_team_admin,
)
from backend.authz import enforce, role_resolver
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.identity import users as user_service
from backend.services.org import (
    LastActiveAdminError,
    MembershipError,
    membership_in,
    remove_from_org,
)
from backend.services.org import memberships as membership_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service

router = APIRouter(prefix="/api/v1/teams/{team_id}/memberships", tags=["memberships"])


class MembershipUpdate(BaseModel):
    role: TeamRole


class MembershipMove(BaseModel):
    target_team_id: UUID
    role: TeamRole | None = None


async def _enforce_role_write(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    *,
    team_id: UUID,
    user_id: UUID,
    action: Action,
    requested: TeamRole,
) -> None:
    """Decide the role write against what reaches ``user_id`` on ``team_id``
    from above, and leave the decision on record. Admin by descent is the
    team above's fact: a row here can add direct admin standing but never
    lower or restate it (the policy's branch table says which)."""
    roles = await role_resolver(request, db, ctx).for_team(team_id)
    descent = await membership_service.descent_for(db, team_id=team_id, user_id=user_id)
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(
            ResourceType.TEAM_MEMBERSHIP,
            id=f"{team_id}:{user_id}",
            org_id=ctx.org_id,
            team_id=team_id,
        ),
        {
            "in_org": roles.in_org,
            "roles": roles.roles,
            "requested_role": requested,
            "descent_role": descent.role if descent is not None else None,
            "descent_from": descent.from_team.name if descent is not None else None,
        },
    )


async def _team_in_org(db: AsyncSession, team_id: UUID, org_id: UUID) -> None:
    """404 unless `team_id` is in the request's org. Walks the FULL ancestor
    chain (any depth), so grandchild teams resolve correctly — matches the
    permission-descent model and the identical check in the teams router."""
    if not await team_service.belongs_to_org(db, team_id, org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")


async def _belongs_to_org(db: AsyncSession, user_id: UUID, org_id: UUID) -> User | None:
    """The identity behind ``user_id`` when it holds a membership in ``org_id``
    (active or not), else None. Belonging is the org's membership row; an
    identity that only belongs to another org is a stranger here."""
    if await org_membership_service.get(db, user_id=user_id, org_team_id=org_id) is None:
        return None
    return await user_service.get_by_id(db, user_id)


@router.get("", response_model=list[TeamMembershipRead])
async def list_members(
    team_id: UUID, caller: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> list[TeamMembershipRead]:
    await _team_in_org(db, team_id, org_id)
    rows = await membership_service.list_for_team(db, team_id)
    return [TeamMembershipRead.model_validate(m) for m in rows]


@router.post(
    "",
    response_model=TeamMembershipRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_team_admin()), Depends(require_email_verified)],
)
@names_body_errors(
    "invalid_membership",
    (
        "A membership names the person and the team: send user_id, team_id (the same "
        "team as the URL) and a role of admin or member."
    ),
)
async def add_membership(
    request: Request,
    team_id: UUID,
    payload: TeamMembershipCreate,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> TeamMembershipRead:
    await _team_in_org(db, team_id, org_id)
    if payload.team_id != team_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The team in the request doesn't match the URL",
        )
    target_user = (
        await user_service.get_by_id(db, payload.user_id)
        if await org_membership_service.active(db, user_id=payload.user_id, org_team_id=org_id)
        is not None
        else None
    )
    if target_user is None:
        # Only an active member of this org can be seated on one of its teams;
        # anybody else, whatever other org they are in, is refused as a 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not a member of this organization",
        )
    await _enforce_role_write(
        request,
        db,
        ctx,
        team_id=team_id,
        user_id=payload.user_id,
        action=Action.CREATE,
        requested=payload.role,
    )
    try:
        membership = await membership_service.add_member(
            db,
            team_id=team_id,
            user_id=payload.user_id,
            role=payload.role,
            actor=ctx.audit_dict(),
        )
    except MembershipError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    emit_event(
        EventName.membership_added,
        user_id=payload.user_id,
        org_id=org_id,
        role=str(payload.role),
    )
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="membership.added",
        target=target_user.email,
        detail={"team_id": str(team_id), "role": payload.role.value},
    )
    return TeamMembershipRead.model_validate(membership)


@router.patch(
    "/{user_id}",
    response_model=TeamMembershipRead,
    dependencies=[Depends(require_team_admin()), Depends(require_email_verified)],
)
async def change_membership_role(
    request: Request,
    team_id: UUID,
    user_id: UUID,
    payload: MembershipUpdate,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> TeamMembershipRead:
    await _team_in_org(db, team_id, org_id)
    membership = await membership_service.get(
        db, team_id=team_id, user_id=user_id, org_team_id=org_id
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
    await _enforce_role_write(
        request,
        db,
        ctx,
        team_id=team_id,
        user_id=user_id,
        action=Action.WRITE,
        requested=payload.role,
    )
    try:
        updated = await membership_service.change_role(
            db, membership, payload.role, actor=ctx.audit_dict()
        )
    except MembershipError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    member = await user_service.get_by_id(db, user_id)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="membership.role_changed",
        target=member.email if member is not None else str(user_id),
        detail={"team_id": str(team_id), "role": payload.role.value},
    )
    return TeamMembershipRead.model_validate(updated)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_team_admin()), Depends(require_email_verified)],
)
async def remove_membership(
    team_id: UUID,
    user_id: UUID,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> None:
    """Remove a user from a team.

    On the ORG ROOT this is removal from the org, not a team edit: the person's
    membership in the org goes (and with it every team seat they hold here),
    every credential they hold in the org is ended, and any invitation they
    still have outstanding into the org is cancelled. Dropping only the team
    rows would leave them a member, with continued read access to everything
    scoped to the org and continued spend against its pool. Their identity and
    every other org they belong to are untouched.
    """
    await _team_in_org(db, team_id, org_id)
    membership = await membership_service.get(
        db, team_id=team_id, user_id=user_id, org_team_id=org_id
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
    org_membership = await membership_in(db, user_id=user_id, org_team_id=org_id)
    member = await user_service.get_by_id(db, user_id) if org_membership is not None else None
    if org_membership is None or member is None:
        # A team row proves the TEAM is ours, not the person's standing in the
        # org: refuse a target with no membership in the caller's org, the same
        # way the sibling handlers refuse a target outside it.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    deprovisioning = team_id == org_id
    if deprovisioning and user_id == caller.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove yourself from the organization",
        )
    if deprovisioning:
        try:
            await remove_from_org(db, org_membership, actor=ctx.audit_dict())
        except LastActiveAdminError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        await record_security_event(
            db, user_id=user_id, event="auth.org_removed", org_team_id=org_id
        )
    else:
        try:
            await membership_service.remove_member(db, membership, actor=ctx.audit_dict())
        except MembershipError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    emit_event(EventName.membership_removed, user_id=user_id, org_id=org_id)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="membership.removed",
        target=member.email,
        detail={"team_id": str(team_id), "deprovisioned": deprovisioning},
    )


@router.post(
    "/{user_id}/move",
    response_model=TeamMembershipRead,
    dependencies=[Depends(require_team_admin()), Depends(require_email_verified)],
)
async def move_membership(
    team_id: UUID,
    user_id: UUID,
    payload: MembershipMove,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> TeamMembershipRead:
    """Move a user from this team to another team in the same org. Requires
    team-admin on BOTH the source (route dep) and the target (checked here);
    org-admin satisfies both via permission descent."""
    await _team_in_org(db, team_id, org_id)
    await _team_in_org(db, payload.target_team_id, org_id)
    if not await _is_admin_of_team_or_ancestor(
        db, user_id=caller.id, team_id=payload.target_team_id, org_team_id=org_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Team admin role required for the target team",
        )
    try:
        moved = await membership_service.move_member(
            db,
            user_id=user_id,
            from_team_id=team_id,
            to_team_id=payload.target_team_id,
            role=payload.role,
            actor=ctx.audit_dict(),
        )
    except MembershipError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    emit_event(
        EventName.membership_added,
        user_id=user_id,
        org_id=org_id,
        role=str(moved.role),
    )
    return TeamMembershipRead.model_validate(moved)
