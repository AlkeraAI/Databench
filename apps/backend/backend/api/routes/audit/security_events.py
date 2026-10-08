"""The caller's own identity security log.

What happened to the caller's account itself, in any org: sign-ins and failed
sign-ins, password and email changes, MFA, lockouts, logouts and ended
sessions, an org deactivating, removing or re-roling the caller, platform bans
and disables.
Readable by the person it belongs to and nobody else on this surface; an org
admin's audit view never shows it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from backend.auth.account_authority import speaks_for_account
from backend.auth.dependencies import CurrentOrg, CurrentUser, DbSession
from backend.services.audit import SECURITY_EVENTS_PAGE_MAX, security_events_of

router = APIRouter(prefix="/api/v1/me", tags=["me-security"])


class SecurityEventRead(BaseModel):
    """One event in the caller's security log. ``org_team_id`` is the org the
    caller's session was in when it happened, when there was one."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event: str
    org_team_id: UUID | None
    ip_prefix: str | None
    user_agent: str | None
    detail: dict[str, Any] | None
    created_at: datetime


class SecurityEventPage(BaseModel):
    events: list[SecurityEventRead]
    # Pass as ``before`` and ``before_id`` for the next (older) page; both null
    # on the last page.
    next_before: datetime | None
    next_before_id: UUID | None = None


def _within_org(event: SecurityEventRead, org_id: UUID) -> SecurityEventRead:
    """``event`` with every other org's id taken out: its own org, and any
    detail field that names an org (``*_org_team_id``)."""
    own = str(org_id)
    detail = (
        {
            key: value
            for key, value in event.detail.items()
            if not (key.endswith("org_team_id") and value != own)
        }
        if event.detail is not None
        else None
    )
    return event.model_copy(
        update={
            "org_team_id": event.org_team_id if event.org_team_id == org_id else None,
            "detail": detail,
        }
    )


@router.get("/security-events", response_model=SecurityEventPage)
async def list_my_security_events(
    request: Request,
    user: CurrentUser,
    org_id: CurrentOrg,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=SECURITY_EVENTS_PAGE_MAX),
    before: datetime | None = Query(default=None),
    before_id: UUID | None = Query(default=None),
) -> SecurityEventPage:
    """The caller's security events, newest first, one page at a time. The
    next page starts after the last row of this one (``next_before`` and
    ``next_before_id``), so events sharing a timestamp are never skipped. A
    browser session an org's IdP started reads them without any other org's
    id."""
    rows = await security_events_of(db, user.id, limit=limit, before=before, before_id=before_id)
    events = [SecurityEventRead.model_validate(row) for row in rows]
    if not await speaks_for_account(db, request, user):
        events = [_within_org(event, org_id) for event in events]
    last = events[-1] if len(events) == limit else None
    return SecurityEventPage(
        events=events,
        next_before=last.created_at if last is not None else None,
        next_before_id=last.id if last is not None else None,
    )


__all__ = ["router"]
