"""The signed-in person's own orgs: found another, leave the current one.

- POST /api/v1/orgs                  — create an org the caller owns
- POST /api/v1/orgs/current/leave    — leave the org the credential is in

Both are served only while multi-org is on (a 404 otherwise, with nothing
written), and both take a browser session only: founding an org and leaving
one are a person's own decisions, made in the portal, never by a CLI token,
an agent, a personal access token or a machine. Neither takes an org from
the client: founding names no existing org, and leaving acts on the org the
credential is in.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.identity.membership import (
    CreateOrgRequest,
    LeaveOrgResponse,
    OrgCreatedResponse,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status

from backend.api.rate_limit import limited
from backend.auth.dependencies import (
    BrowserSessionUser,
    CurrentMember,
    CurrentPrincipal,
    DbSession,
    require_email_verified,
)
from backend.auth.session_issue import client_hint
from backend.services.org import (
    LastActiveAdminError,
    OrgCreationLimitedError,
    found_org,
    leave_org,
    next_org_after_leaving,
)

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


async def require_multi_org() -> None:
    """The route exists only while multi-org is on."""
    if not multi_org_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.post(
    "",
    response_model=OrgCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_multi_org),
        # A squatter on an unproven address must not be able to found orgs
        # under it.
        Depends(require_email_verified),
        Depends(limited("mutation")),
    ],
)
async def create_org(
    payload: CreateOrgRequest, request: Request, caller: BrowserSessionUser, db: DbSession
) -> OrgCreatedResponse:
    """Create an org with the caller as its owner. The caller's session stays
    in the org it is in; the client switches into the new one."""
    try:
        org = await found_org(
            db,
            user=caller,
            name=payload.name,
            now=datetime.now(UTC),
            client=client_hint(request),
        )
    except OrgCreationLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "org_creation_limited",
                "message": "You've created too many organizations recently. Try again later.",
            },
        ) from exc
    emit_event(EventName.org_created, user_id=caller.id, org_id=org.id)
    return OrgCreatedResponse(org_team_id=org.id, org_name=org.name)


@router.post(
    "/current/leave",
    response_model=LeaveOrgResponse,
    dependencies=[Depends(require_multi_org), Depends(limited("mutation"))],
)
async def leave_current_org(
    request: Request,
    caller: BrowserSessionUser,
    member: CurrentMember,
    ctx: CurrentPrincipal,
    db: DbSession,
) -> LeaveOrgResponse:
    """Leave the org this session is in. Every credential the caller holds in
    it ends; their other orgs are untouched. The answer names the org to
    switch into next and signs nothing in."""
    try:
        await leave_org(
            db,
            user=caller,
            membership=member.membership,
            actor=ctx.audit_dict(),
            client=client_hint(request),
        )
    except LastActiveAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_admin",
                "message": (
                    "You're the only admin of this organization. "
                    "Make someone else an admin before you leave."
                ),
            },
        ) from exc
    return LeaveOrgResponse(
        next_org_team_id=await next_org_after_leaving(db, caller, left=member.org_id)
    )
