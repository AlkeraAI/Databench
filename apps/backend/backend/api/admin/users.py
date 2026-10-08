"""Admin: cross-tenant user operations.

Read + non-privilege patch open to ALKERA_SUPPORT.
Granting/revoking platform_role, and the identity-level disable, are their own
endpoints, gated to ALKERA_ADMIN.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from alkera_core.abuse import is_disposable_domain
from alkera_core.account import requests as account_requests
from alkera_core.account.erasure import revoke_credentials
from alkera_core.auth.tenancy import home_org_id
from alkera_core.authz import Action
from alkera_core.email import account as account_email
from alkera_core.email import send_in_background
from alkera_core.models import Team
from alkera_core.org_entitlements import org_entitlements
from alkera_core.schemas.account import (
    AdminAccountRead,
    AdminDeletionBody,
    DeletionStateRead,
    DeletionStatusRead,
)
from alkera_core.schemas.identity.admin_users import AdminUserIpInfoRead, AdminUserRow
from alkera_core.schemas.identity.user import UserRead
from alkera_core.schemas.tenancy.org import PlatformRoleUpdate
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.admin._audit import AuditedRoute
from backend.api.deps.account import account_target, authorize_account
from backend.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    require_platform_admin,
)
from backend.services import identity as identity_services
from backend.services.abuse import bans as ban_service
from backend.services.abuse import ip_info as ip_info_service
from backend.services.audit import record_security_event
from backend.services.identity import users as user_service
from backend.services.infra import nudge_account_lifecycle_sweep

router = APIRouter(prefix="/users", route_class=AuditedRoute)


class AdminUserUpdate(BaseModel):
    """Fields a Support-tier admin may edit cross-tenant.

    Deliberately *cosmetic only*. Admin/staff can NEVER reset a user's password
    or change their email — both are account-takeover vectors (a password reset
    is a direct hijack; an email change re-routes the self-serve reset link).
    Password changes are the user's own action via the self-serve reset flow.
    Platform role is its own ADMIN-gated endpoint.
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=255)
    last_name: str | None = Field(default=None, min_length=1, max_length=255)


@router.get("", response_model=list[AdminUserRow])
async def list_users(db: DbSession) -> list[AdminUserRow]:
    """The cross-tenant user register, newest first, with the abuse-forensics
    columns the console monitors: verification state, signup/login IPs,
    disposable-domain flag, and month-to-date settled spend (one grouped scan
    merged in, not a query per row)."""
    rows = await user_service.list_all(db)
    spend = await org_entitlements().user_spend_this_month(db, now=datetime.now(UTC))
    # Active bans (by account or by the address's domain), one statement.
    ban_reasons = await ban_service.ban_reasons(db)
    org_ids = {home_org_id(u) for u in rows}
    org_rows = (await db.execute(select(Team.id, Team.name).where(Team.id.in_(org_ids)))).all()
    org_names: dict[UUID, str] = {team_id: name for team_id, name in org_rows}
    out: list[AdminUserRow] = []
    for u in sorted(rows, key=lambda r: r.created_at, reverse=True):
        user_spend = spend.get(u.id)
        out.append(
            AdminUserRow(
                id=u.id,
                email=u.email,
                display_name=u.display_name,
                org_team_id=home_org_id(u),
                org_name=org_names.get(home_org_id(u)),
                platform_role=u.platform_role,
                is_active=u.is_active,
                created_at=u.created_at,
                email_verified_at=u.email_verified_at,
                disposable_email=is_disposable_domain(u.email_domain),
                signup_ip=u.signup_ip,
                last_login_ip=u.last_login_ip,
                mtd_billed_nanos=user_spend.billed_nanos if user_spend else 0,
                mtd_request_count=user_spend.request_count if user_spend else 0,
                banned=u.id in ban_reasons,
                ban_reason=ban_reasons.get(u.id),
            )
        )
    return out


@router.get("/{user_id}/ip-info", response_model=AdminUserIpInfoRead)
async def user_ip_info(user_id: UUID, db: DbSession) -> AdminUserIpInfoRead:
    """Best-effort geo/network context for the user's recorded IPs (external
    lookup, cached per IP; degrades to an ``error`` field offline)."""
    target = await user_service.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return AdminUserIpInfoRead(
        signup=await ip_info_service.lookup(target.signup_ip),
        last_login=await ip_info_service.lookup(target.last_login_ip),
    )


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(user_id: UUID, payload: AdminUserUpdate, db: DbSession) -> UserRead:
    target = await user_service.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    updated = await user_service.update_profile(
        db,
        target,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )
    return UserRead.model_validate(updated)


# ADMIN-only escalation — granting/revoking platform_role is the only way
# to escalate Alkera staff privileges, so it lives behind its own gate.
@router.patch(
    "/{user_id}/platform_role",
    response_model=UserRead,
    dependencies=[Depends(require_platform_admin)],
)
async def set_platform_role(user_id: UUID, payload: PlatformRoleUpdate, db: DbSession) -> UserRead:
    target = await user_service.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    updated = await user_service.set_platform_role(db, target, payload.platform_role)
    return UserRead.model_validate(updated)


class AdminUserActiveUpdate(BaseModel):
    active: bool


# ADMIN-only escalation — the identity-level disable signs a person out of
# every org at once, which no org may do.
@router.put(
    "/{user_id}/active",
    response_model=UserRead,
    dependencies=[Depends(require_platform_admin)],
)
async def set_user_active(
    user_id: UUID, payload: AdminUserActiveUpdate, db: DbSession, ctx: CurrentPrincipal
) -> UserRead:
    """Disable (or re-enable) an identity platform-wide: it signs in nowhere,
    every credential it holds is ended and its live chats end, in every org.
    Its memberships are untouched, so re-enabling restores exactly what each
    org had decided."""
    target = await user_service.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.is_active != payload.active:
        await user_service.set_active(db, target, payload.active, actor=ctx.audit_dict())
        await record_security_event(
            db,
            user_id=target.id,
            event="platform.user_enabled" if payload.active else "platform.user_disabled",
        )
    return UserRead.model_validate(target)


# --------------------------------------------------------------------------- #
# Support tool: a person's deletion requests
# --------------------------------------------------------------------------- #


@router.get("/{user_id}/account", response_model=AdminAccountRead)
async def account_requests_read(
    user_id: UUID, request: Request, db: DbSession, staff: CurrentUser, ctx: CurrentPrincipal
) -> AdminAccountRead:
    """Where a person's deletion requests stand. Support and admins."""
    target = await account_target(db, user_id)
    await authorize_account(request, db, ctx, staff, target, Action.READ)
    latest = await account_requests.latest_deletion(db, target.id)
    return AdminAccountRead(
        user_id=target.id,
        deleted_at=target.deleted_at,
        deletion=identity_services.deletion_read(latest) if latest else None,
    )


@router.post(
    "/{user_id}/account/deletion",
    response_model=DeletionStatusRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def account_deletion_for(
    user_id: UUID,
    payload: AdminDeletionBody,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    staff: CurrentUser,
    ctx: CurrentPrincipal,
) -> DeletionStatusRead:
    """Schedule a deletion a person asked for through support, with the normal
    grace window, or (``immediate``) due now for a request already verified and
    past its own window. The person is emailed either way."""
    target = await account_target(db, user_id)
    await authorize_account(request, db, ctx, staff, target, Action.DELETE)
    if target.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The account was deleted")
    try:
        scheduled = await account_requests.schedule_deletion(
            db,
            target.id,
            requested_by=staff.id,
            source="support",
            grace=timedelta(0) if payload.immediate else None,
        )
    except account_requests.DeletionAlreadyScheduledError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "deletion_already_scheduled",
                "message": "A deletion is already scheduled.",
            },
        ) from exc
    except account_requests.DeletionBlockedError as exc:
        presented = await identity_services.present_plan(db, target, exc.plan)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "deletion_blocked",
                "message": "The account has blockers to settle first.",
                "blockers": [b.model_dump(mode="json") for b in presented.blockers],
            },
        ) from exc
    await revoke_credentials(db, target.id)
    await record_security_event(db, user_id=target.id, event="account.deletion_requested")
    read = identity_services.deletion_read(scheduled)
    email, name, purge_after = target.email, target.display_name, scheduled.purge_after
    await db.commit()
    send_in_background(
        account_email.send_deletion_scheduled(email, name=name, purge_after=purge_after),
        name="email.account_deletion_scheduled",
    )
    if payload.immediate:
        background.add_task(nudge_account_lifecycle_sweep)
    return read


@router.delete("/{user_id}/account/deletion", response_model=DeletionStateRead)
async def account_deletion_cancel_for(
    user_id: UUID, request: Request, db: DbSession, staff: CurrentUser, ctx: CurrentPrincipal
) -> DeletionStateRead:
    """Cancel a person's scheduled deletion on their behalf."""
    target = await account_target(db, user_id)
    await authorize_account(request, db, ctx, staff, target, Action.DELETE)
    cancelled = await account_requests.cancel_deletion(db, target.id)
    if cancelled is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "no_deletion_scheduled", "message": "No deletion is scheduled."},
        )
    await record_security_event(db, user_id=target.id, event="account.deletion_cancelled")
    email, name = target.email, target.display_name
    await db.commit()
    send_in_background(
        account_email.send_deletion_cancelled(email, name=name),
        name="email.account_deletion_cancelled",
    )
    return DeletionStateRead(request=None)
