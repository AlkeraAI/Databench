"""Consolidated user dashboard endpoint.

The SPA polls `/api/v1/dashboard` to refresh the user's home view in one
request. Headroom: add fields (notifications, announcements, etc.) here
as features land.
"""

from __future__ import annotations

from alkera_core.config import settings
from alkera_core.entitlements import entitled_feature_names
from alkera_core.models import Team
from alkera_core.org_entitlements import org_entitlements
from alkera_core.schemas.identity.invitation import InvitationRead
from alkera_core.schemas.identity.user import UserRead
from alkera_core.schemas.system.dashboard import DashboardResponse
from alkera_core.schemas.tenancy.team import TeamRead
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from backend.auth.dependencies import CurrentOrg, CurrentUser, DbSession, user_is_org_admin
from backend.services.org import invitations as invitation_service
from backend.services.org import teams as team_service

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    caller: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> DashboardResponse:
    org = await team_service.get_by_id(db, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User has no org")

    # Teams the user is a member of (any role).
    from alkera_core.models import TeamMembership

    rows = await db.execute(
        select(Team)
        .join(TeamMembership, TeamMembership.team_id == Team.id)
        .where(TeamMembership.user_id == caller.id, TeamMembership.org_team_id == org_id)
        .order_by(Team.created_at)
    )
    teams = list(rows.scalars().all())

    pending = await invitation_service.list_pending_for_email(db, caller.email, org_team_id=org_id)

    # member_count is a computed (non-column) field on TeamRead; populate it here
    # too so the dashboard payload doesn't advertise a constant 0.
    counts = await team_service.member_counts(db, [org.id, *(t.id for t in teams)])

    def _team_read(team: Team) -> TeamRead:
        return TeamRead.model_validate(team).model_copy(
            update={"member_count": counts.get(team.id, 0)}
        )

    return DashboardResponse(
        # The user's org on this payload is the request's: a person in two orgs
        # reads each org's dashboard with that org named, never their first one.
        user=UserRead.model_validate(caller).model_copy(update={"org_team_id": org_id}),
        org=_team_read(org),
        teams=[_team_read(t) for t in teams],
        pending_invitations=[InvitationRead.model_validate(p) for p in pending],
        is_org_admin=await user_is_org_admin(db, caller, org_team_id=org_id),
        # SaaS and unentitled installs are byte-identical ([]) — the gated
        # surfaces must look nonexistent, not locked.
        entitled_features=entitled_feature_names() if settings.is_self_hosted else [],
        enterprise_features_enabled=await org_entitlements().enterprise_features(db, org_id),
    )
