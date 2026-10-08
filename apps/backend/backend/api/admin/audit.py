"""Admin audit log — read the trail of Alkera-staff write actions.

Viewing is ADMIN-only (above the router's support floor): support performs
audited actions but only ALKERA_ADMIN can read the log. Entries are written by
`AuditedRoute`, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.schemas.system.audit_log import AuditLogPage, AuditLogRead
from fastapi import APIRouter, Depends, Query

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import DbSession, require_platform_admin
from backend.services.audit import audit_log as audit_log_service

router = APIRouter(prefix="/audit-logs", route_class=AuditedRoute)


def _utc(bound: datetime | None) -> datetime | None:
    """A bound with no offset reads as UTC, matching the stored timestamps."""
    if bound is None or bound.tzinfo is not None:
        return bound
    return bound.replace(tzinfo=UTC)


@router.get(
    "",
    response_model=AuditLogPage,
    dependencies=[Depends(require_platform_admin)],
)
async def list_audit_logs(
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str | None = Query(default=None, max_length=128, description="Exact action"),
    actor_email: str | None = Query(default=None, max_length=320),
    created_after: datetime | None = Query(
        default=None, description="Inclusive lower bound; no offset reads as UTC"
    ),
    created_before: datetime | None = Query(
        default=None, description="Inclusive upper bound; no offset reads as UTC"
    ),
) -> AuditLogPage:
    filters = audit_log_service.AuditLogFilters(
        action=action,
        actor_email=actor_email,
        created_after=_utc(created_after),
        created_before=_utc(created_before),
    )
    rows, total = await audit_log_service.list_page(
        db, offset=(page - 1) * page_size, limit=page_size, filters=filters
    )
    return AuditLogPage(
        items=[AuditLogRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        actions=await audit_log_service.recorded_actions(db),
    )
