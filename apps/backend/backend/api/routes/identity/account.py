"""The person's own account lifecycle: delete the account.

Mounted outside the email-verification gate: the right to erasure does not wait
on a verified address.

Every route decides through ``enforce()`` on the account resource first; only
the person, acting on their own browser session, gets through (an agent, a CLI
token or an access key is refused, on record).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alkera_core.account import requests as account_requests
from alkera_core.account.erasure import revoke_credentials
from alkera_core.auth import COOKIE_NAME
from alkera_core.authz import Action
from alkera_core.config import settings
from alkera_core.email import account as account_email
from alkera_core.email import send_in_background
from alkera_core.models import User
from alkera_core.schemas.account import (
    DeletionPlanRead,
    DeletionRequestBody,
    DeletionStateRead,
    DeletionStatusRead,
)
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.account import authorize_self
from backend.api.session_responses import clear_session_cookies
from backend.auth.account_authority import account_standing, account_wide_controls_guarded
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    optional_session_claims,
)
from backend.auth.session_issue import METHOD_RENEWAL, reissue_session
from backend.services import identity as identity_services
from backend.services.audit import record_security_event

router = APIRouter(prefix="/api/v1/me/account", tags=["account"])


def _signed_in_within(request: Request, window: timedelta) -> bool:
    claims = optional_session_claims(request)
    if claims is None:
        return False
    return datetime.now(UTC) - datetime.fromtimestamp(claims.issued_at, tz=UTC) <= window


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": message}
    )


async def require_deletion_proof(
    db: AsyncSession, request: Request, user: User, payload: DeletionRequestBody
) -> None:
    """Refuse a deletion request that does not prove it is the person.

    The typed email must match. A password account proves its password (and
    its authenticator code when MFA is on), counted against the login lockout.
    An account with no password must have signed in recently, as the person
    (``reauth_required`` otherwise), and still prove its code when MFA is on."""
    if payload.confirm_email.strip().lower() != user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "confirm_email_mismatch",
                "message": "Type your account's email exactly to confirm.",
            },
        )
    try:
        if user.password_hash is not None:
            await identity_services.require_current_factors(
                db,
                user,
                current_password=payload.current_password,
                mfa_code=payload.mfa_code,
                federated_message="Sign in again to delete your account.",
                missing_factor_message="Enter your password to delete your account.",
            )
            return
        window = timedelta(seconds=settings.account_reauth_window_seconds)
        if account_wide_controls_guarded():
            fresh = (await account_standing(db, request, user)).within(window)
        else:
            fresh = _signed_in_within(request, window)
        if not fresh:
            raise _forbidden("reauth_required", "Sign in again to delete your account.")
        await identity_services.require_mfa_code(db, user, mfa_code=payload.mfa_code)
    except identity_services.StepUpRefusedError as refused:
        raise _forbidden(refused.code, refused.message) from refused


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #


@router.get("/deletion/plan", response_model=DeletionPlanRead)
async def deletion_plan(
    request: Request, user: CurrentUser, ctx: CurrentPrincipal, db: DbSession
) -> DeletionPlanRead:
    """What deleting the account would do, as the rows stand now: each org's
    fate, who receives shared items, and what must be settled first."""
    await authorize_self(request, db, ctx, user, Action.READ)
    return await identity_services.plan_for(db, user)


@router.get("/deletion", response_model=DeletionStateRead)
async def deletion_state(
    request: Request, user: CurrentUser, ctx: CurrentPrincipal, db: DbSession
) -> DeletionStateRead:
    await authorize_self(request, db, ctx, user, Action.READ)
    live = await account_requests.live_deletion(db, user.id)
    return DeletionStateRead(request=identity_services.deletion_read(live) if live else None)


@router.post("/deletion", response_model=DeletionStatusRead, status_code=status.HTTP_202_ACCEPTED)
async def request_deletion(
    payload: DeletionRequestBody,
    request: Request,
    response: Response,
    user: CurrentUser,
    org_id: CurrentOrg,
    ctx: CurrentPrincipal,
    db: DbSession,
) -> DeletionStatusRead:
    """Schedule the account's deletion after the grace window.

    The person types their email and proves a current factor. Every other
    credential they hold ends at once (sessions elsewhere, CLI tokens, access
    tokens); this browser stays signed in so they can cancel. 409 with the
    blockers when something must be settled first."""
    await authorize_self(request, db, ctx, user, Action.DELETE)
    await require_deletion_proof(db, request, user, payload)
    try:
        scheduled = await account_requests.schedule_deletion(
            db, user.id, requested_by=user.id, source="self"
        )
    except account_requests.DeletionAlreadyScheduledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "deletion_already_scheduled",
                "message": "Your account is already scheduled for deletion.",
                "purge_after": exc.request.purge_after.isoformat(),
            },
        ) from exc
    except account_requests.DeletionBlockedError as exc:
        presented = await identity_services.present_plan(db, user, exc.plan)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "deletion_blocked",
                "message": "Settle these first: " + " ".join(b.message for b in presented.blockers),
                "blockers": [b.model_dump(mode="json") for b in presented.blockers],
            },
        ) from exc
    await revoke_credentials(db, user.id)
    await record_security_event(db, user_id=user.id, event="account.deletion_requested")
    read = identity_services.deletion_read(scheduled)
    email, name, purge_after = user.email, user.display_name, scheduled.purge_after
    await db.commit()
    if request.cookies.get(COOKIE_NAME):
        await reissue_session(
            db, user, request=request, response=response, method=METHOD_RENEWAL, org_team_id=org_id
        )
    else:
        clear_session_cookies(response)
    send_in_background(
        account_email.send_deletion_scheduled(email, name=name, purge_after=purge_after),
        name="email.account_deletion_scheduled",
    )
    return read


@router.delete("/deletion", response_model=DeletionStateRead)
async def cancel_deletion(
    request: Request, user: CurrentUser, ctx: CurrentPrincipal, db: DbSession
) -> DeletionStateRead:
    """Cancel the scheduled deletion. 404 when none is scheduled."""
    await authorize_self(request, db, ctx, user, Action.DELETE)
    cancelled = await account_requests.cancel_deletion(db, user.id)
    if cancelled is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "no_deletion_scheduled", "message": "No deletion is scheduled."},
        )
    await record_security_event(db, user_id=user.id, event="account.deletion_cancelled")
    email, name = user.email, user.display_name
    await db.commit()
    send_in_background(
        account_email.send_deletion_cancelled(email, name=name),
        name="email.account_deletion_cancelled",
    )
    return DeletionStateRead(request=None)


__all__ = ["router"]
