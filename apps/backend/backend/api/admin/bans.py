"""Admin: platform bans on users and on email domains.

Platform ADMIN only, for the register as much as for a change — decided by the
``platform.ban`` policy through ``enforce``, so a support caller gets the
policy's refusal with a decision row rather than the router floor's 403. The
resource is platform-scoped (no ``org_id``): the caller is staff acting on
somebody else's tenant, and binding the resource to that tenant would trip
the engine's cross-org guard before the policy ran.

Refusals about the TARGET are input errors, not authority: banning oneself or
a staff account is a 422, a second ban while one is active a 409, lifting
where nothing is active a 404. A user ban also lands in the banned person's
org audit trail with the acting chain; every write lands in the platform
audit log through ``AuditedRoute`` like the rest of this router.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.bans import InvalidDomainError, normalize_domain
from alkera_core.models import EmailDomainBan, PlatformRole, User, UserBan
from alkera_core.schemas.identity.bans import (
    DomainBanCreate,
    DomainBanRead,
    UserBanCreate,
    UserBanRead,
)
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.api.params import PathId
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services.abuse import BanConflictError, BanRefusedError, NoActiveBanError
from backend.services.abuse import bans as ban_service
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.identity import users as user_service
from backend.services.org import org_ids_of

router = APIRouter(prefix="/bans", route_class=AuditedRoute)

_RESOURCE_TYPE = ResourceType.PLATFORM_BAN


async def _decide(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    action: Action,
    kind: str,
    operation: str,
    resource_id: str,
) -> None:
    role = caller.platform_role
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(_RESOURCE_TYPE, id=resource_id),
        {
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "kind": kind,
            "operation": operation,
        },
    )


def _refused(exc: BanRefusedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "ban_refused", "message": str(exc)},
    )


def _user_ban_read(ban: UserBan, *, target: User, created_by: User) -> UserBanRead:
    return UserBanRead(
        id=ban.id,
        user_id=target.id,
        user_email=target.email,
        user_display_name=target.display_name,
        reason=ban.reason,
        created_at=ban.created_at,
        created_by_id=created_by.id,
        created_by_email=created_by.email,
        lifted_at=None,
        lifted_by_id=None,
        lifted_by_email=None,
        active=True,
    )


def _domain_ban_read(ban: EmailDomainBan, *, created_by: User) -> DomainBanRead:
    return DomainBanRead(
        id=ban.id,
        domain=ban.domain,
        reason=ban.reason,
        created_at=ban.created_at,
        created_by_id=created_by.id,
        created_by_email=created_by.email,
        lifted_at=None,
        lifted_by_id=None,
        lifted_by_email=None,
        active=True,
    )


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #


@router.get("/users", response_model=list[UserBanRead])
async def list_user_bans(
    request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> list[UserBanRead]:
    """Every user ban ever written, active and lifted, newest first."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.READ,
        kind="user",
        operation="list",
        resource_id="users",
    )
    return await ban_service.list_user_bans(db)


@router.post("/users", response_model=UserBanRead, status_code=status.HTTP_201_CREATED)
async def ban_user(
    payload: UserBanCreate,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> UserBanRead:
    """Ban a user: every session they hold is revoked, and every path that
    resolves them answers as if the account did not exist."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        kind="user",
        operation="ban",
        resource_id=str(payload.user_id),
    )
    target = await user_service.get_by_id(db, payload.user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        ban = await ban_service.ban_user(db, target=target, actor=caller, reason=payload.reason)
    except BanRefusedError as exc:
        raise _refused(exc) from exc
    except BanConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # A ban is the identity's (it is refused everywhere): the identity's own
    # security log carries it in full, and each org the person belongs to gets
    # the fact in its own chain.
    await record_security_event(
        db, user_id=target.id, event="platform.user_banned", detail={"reason": payload.reason}
    )
    await _tell_each_org(db, target, "platform.user_banned", caller=caller, ctx=ctx)
    return _user_ban_read(ban, target=target, created_by=caller)


async def _tell_each_org(
    db: DbSession, target: User, action: str, *, caller: User, ctx: ActingContext
) -> None:
    """Record a ban or a lift in the chain of every org the person belongs to,
    so an org's compliance review still sees what Alkera did to one of its
    members. Each row carries what that org may know: who acted and on which
    member. The free-text reason is the platform's (it may describe conduct in
    another org) and stays in the identity's own log; no row names another
    org."""
    for org_id in await org_ids_of(db, target.id):
        await org_audit_service.record(
            db, org_id=org_id, actor=caller, action=action, target=target.email, acting=ctx
        )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def lift_user_ban(
    user_id: UUID,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> None:
    """Lift the user's active ban. 404 when there is none."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        kind="user",
        operation="lift",
        resource_id=str(user_id),
    )
    try:
        await ban_service.lift_user_ban(db, user_id=user_id, actor=caller)
    except NoActiveBanError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    target = await user_service.get_by_id(db, user_id)
    if target is not None:
        await record_security_event(db, user_id=user_id, event="platform.user_ban_lifted")
        await _tell_each_org(db, target, "platform.user_ban_lifted", caller=caller, ctx=ctx)


# --------------------------------------------------------------------------- #
# domains
# --------------------------------------------------------------------------- #


@router.get("/domains", response_model=list[DomainBanRead])
async def list_domain_bans(
    request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> list[DomainBanRead]:
    """Every domain ban ever written, active and lifted, newest first."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.READ,
        kind="domain",
        operation="list",
        resource_id="domains",
    )
    return await ban_service.list_domain_bans(db)


@router.post("/domains", response_model=DomainBanRead, status_code=status.HTTP_201_CREATED)
async def ban_domain(
    payload: DomainBanCreate,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> DomainBanRead:
    """Ban every address at a domain (exact match, lower-cased; a subdomain is
    a different domain). Existing non-staff accounts there lose their sessions
    now; new signups and invitations at the domain are refused."""
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        kind="domain",
        operation="ban",
        resource_id=payload.domain,
    )
    try:
        ban = await ban_service.ban_domain(
            db, domain=payload.domain, actor=caller, reason=payload.reason
        )
    except BanConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _domain_ban_read(ban, created_by=caller)


@router.delete("/domains/{domain}", status_code=status.HTTP_204_NO_CONTENT)
async def lift_domain_ban(
    domain: PathId,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> None:
    """Lift the domain's active ban. 404 when there is none (a string that is
    not a domain can never carry one)."""
    try:
        normalized = normalize_domain(domain)
    except InvalidDomainError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="This domain is not banned"
        ) from exc
    await _decide(
        request,
        db,
        ctx,
        caller,
        action=Action.ADMIN,
        kind="domain",
        operation="lift",
        resource_id=normalized,
    )
    try:
        await ban_service.lift_domain_ban(db, domain=normalized, actor=caller)
    except NoActiveBanError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
