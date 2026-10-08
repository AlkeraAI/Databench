"""Admin: what is going on inside one org — its chats, the errors and
refusals in them, and its audit trail.

Reads only, at the router's platform-staff floor (support and admin alike):
the console's activity tab is how staff answer a customer asking why a chat
stopped. Nothing here returns transcript content beyond an error's own
detail line.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from alkera_core.schemas.system.org_insight import (
    OrgAuditList,
    OrgChatInsightList,
    OrgIssueList,
)
from fastapi import APIRouter, HTTPException, Query, status

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import DbSession
from backend.services.ops import org_insight as org_insight_service
from backend.services.org import teams as team_service

router = APIRouter(prefix="/orgs", route_class=AuditedRoute)


async def _require_org(db: DbSession, org_id: UUID) -> None:
    org = await team_service.get_by_id(db, org_id)
    if org is None or not org.is_root:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")


@router.get("/{org_id}/chats", response_model=OrgChatInsightList)
async def list_org_chats(
    org_id: UUID, db: DbSession, limit: int = Query(50, ge=1, le=200)
) -> OrgChatInsightList:
    await _require_org(db, org_id)
    return OrgChatInsightList(
        items=await org_insight_service.org_chats(db, org_id=org_id, limit=limit)
    )


@router.get("/{org_id}/errors", response_model=OrgIssueList)
async def list_org_errors(
    org_id: UUID,
    db: DbSession,
    since: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> OrgIssueList:
    await _require_org(db, org_id)
    return OrgIssueList(
        items=await org_insight_service.org_issues(
            db,
            org_id=org_id,
            since=since or org_insight_service.default_since(),
            limit=limit,
        )
    )


@router.get("/{org_id}/audit", response_model=OrgAuditList)
async def list_org_audit(
    org_id: UUID, db: DbSession, limit: int = Query(50, ge=1, le=200)
) -> OrgAuditList:
    await _require_org(db, org_id)
    return await org_insight_service.org_audit(db, org_id=org_id, limit=limit)
