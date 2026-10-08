"""The routes that move a browser session between a person's orgs.

- GET  /api/v1/auth/memberships          the orgs the person can enter
- POST /api/v1/auth/memberships/join     accept a pending membership
- POST /api/v1/auth/refresh/org          switch the session into another org
- POST /api/v1/auth/refresh/org/new      no org left: found one and enter it
- POST /api/v1/auth/refresh/org/join     no org left: accept an invitation

The switch and the landing routes live under the refresh cookie's path: the
login session is named by that cookie, never by a client field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from alkera_core.auth import (
    RefreshError,
    revoke_all_for_user,
    revoke_jti,
    rotate_refresh_token,
    sign_in_policy,
)
from alkera_core.auth.refresh import (
    active_org_of,
    family_of_access_token,
    resolve_refresh_token,
)
from alkera_core.auth.tenancy import MembershipRefused, multi_org_enabled
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.events import actor_for_user
from alkera_core.models import AuthRefreshToken, Team, User
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.identity.auth import (
    LoginResponse,
)
from alkera_core.schemas.identity.membership import (
    CreateOrgRequest,
    JoinMembershipRequest,
    JoinOrgRequest,
    MembershipListResponse,
    MembershipRead,
    SwitchOrgRequest,
)
from alkera_core.verification import is_verified
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from backend.api.rate_limit import limited
from backend.api.return_path import sso_login_with_return
from backend.api.session_responses import (
    SWITCH_CSRF_HEADER,
    SWITCH_CSRF_VALUE,
    answer_refresh_error,
    refresh_refused,
    same_identity_or_absent,
    switch_refused,
    user_read,
)
from backend.auth.account_authority import speaks_for_account
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentUser,
    DbSession,
)
from backend.auth.session_issue import (
    acting_session_jti,
    client_hint,
    issue_rotated_session,
)
from backend.services.audit import record_org_audit, record_security_event
from backend.services.identity import (
    enterable_org,
    joinable_org,
    org_role_in,
    org_sso_required_for,
    switchable_orgs,
    user_with_ban,
)
from backend.services.org import (
    InvitationError,
    OrgCreationLimitedError,
    accept_invitation,
    activate_pending_membership,
    found_org,
    invitation_by_token,
    invitation_link_closed_reason,
    masked_invited_address,
    org_root_of,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_auth_throttle = Depends(limited("credential"))


@router.post("/refresh/org", response_model=LoginResponse, dependencies=[_auth_throttle])
async def switch_org(
    payload: SwitchOrgRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse | JSONResponse:
    """Move this browser's login session into another of the person's orgs.

    It lives under the refresh cookie's path, so the session is named by the
    refresh cookie (never by anything the body says about who is asking), and
    the org in the body is only a choice among the caller's own active
    memberships: anything else is the same 404, so the route never says
    whether an org exists. The org's sign-in policy is asked before anything
    moves; a step-up is a 409 naming the org's single sign-on, and nothing is
    rotated. On success the refresh token rotates (reuse ends the family, as on
    the refresh route), the family names the new org, a new access token is
    minted for that membership, and the access token this browser held for the
    old org is revoked, so a tab still in the old org can never act again."""
    raw = request.cookies.get(settings.auth_refresh_cookie_name)
    if not raw:
        raise switch_refused(
            status.HTTP_401_UNAUTHORIZED, code="unauthorized", message="missing refresh token"
        )
    if request.headers.get(SWITCH_CSRF_HEADER) != SWITCH_CSRF_VALUE:
        raise switch_refused(
            status.HTTP_403_FORBIDDEN, code="csrf", message="This request is missing its header."
        )
    try:
        row, user = await resolve_refresh_token(db, raw)
    except RefreshError as exc:
        return await answer_refresh_error(db, request, exc)
    _, banned = await user_with_ban(db, user.id)
    if banned:
        await revoke_all_for_user(db, user.id)
        await db.commit()
        return refresh_refused(request, code="unauthorized", message="user no longer exists")
    previous = same_identity_or_absent(request, user.id)

    target = payload.org_team_id
    if await enterable_org(db, user, target) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    policy = await sign_in_policy.evaluate(
        db, user=user, org_team_id=target, family_id=row.family_id, method=None
    )
    if isinstance(policy, sign_in_policy.StepUp):
        detail: dict[str, str] = {
            "code": policy.kind,
            "message": (
                "This organization requires single sign-on."
                if policy.kind == "sso_required"
                else "This organization doesn't allow this sign-in method."
            ),
        }
        if policy.login_url is not None:
            detail["login_url"] = sso_login_with_return(
                policy.login_url, return_to=f"/?{urlencode({'switch_org': str(target)})}"
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    left = active_org_of(row, user)
    stale_jtis = {jti for jti in (row.access_jti, previous.jti if previous else None) if jti}
    hint = client_hint(request)
    try:
        minted, user = await rotate_refresh_token(
            db, raw, user_agent=hint.user_agent, ip_prefix=hint.ip_prefix, switch_to=target
        )
    except RefreshError as exc:
        return await answer_refresh_error(db, request, exc)
    except MembershipRefused as exc:
        # The membership ended between the check above and the rotation.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    for jti in stale_jtis:
        await revoke_jti(db, jti)
    claims = await issue_rotated_session(db, user, minted, response=response)
    entered = ActingContext.for_user(
        user_id=user.id, org_id=claims.org_team_id, email=user.email, credential_id=claims.jti
    )
    # The response is in the new org: the echo names it, so the tab learns it.
    request.state.acting_context = entered
    if left != claims.org_team_id:
        # Each org's chain records only its own side of the move: an org never
        # learns which other orgs its members belong to.
        await record_org_audit(
            db,
            org_id=left,
            actor=user,
            action="auth.org_switched",
            target=user.email,
            detail={"switched": "out"},
            acting=ActingContext.for_user(
                user_id=user.id,
                org_id=left,
                email=user.email,
                credential_id=previous.jti if previous else row.access_jti,
            ),
        )
        await record_org_audit(
            db,
            org_id=claims.org_team_id,
            actor=user,
            action="auth.org_switched",
            target=user.email,
            detail={"switched": "in"},
            acting=entered,
        )
        await record_security_event(
            db,
            user_id=user.id,
            event="auth.org_switched",
            org_team_id=claims.org_team_id,
            client=hint,
        )
    return LoginResponse(
        user=await user_read(db, user, claims.org_team_id),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
    )


async def _enter_from_landing(
    db: DbSession,
    request: Request,
    response: Response,
    *,
    raw: str,
    user: User,
    previous_access_jti: str | None,
    org_id: UUID,
) -> LoginResponse | JSONResponse:
    """Move the login session into ``org_id`` (a membership the caller just
    gained) and mint its access token, as a switch would."""
    hint = client_hint(request)
    try:
        minted, user = await rotate_refresh_token(
            db, raw, user_agent=hint.user_agent, ip_prefix=hint.ip_prefix, switch_to=org_id
        )
    except RefreshError as exc:
        return await answer_refresh_error(db, request, exc)
    if previous_access_jti is not None:
        await revoke_jti(db, previous_access_jti)
    claims = await issue_rotated_session(db, user, minted, response=response)
    request.state.acting_context = ActingContext.for_user(
        user_id=user.id, org_id=claims.org_team_id, email=user.email, credential_id=claims.jti
    )
    return LoginResponse(
        user=await user_read(db, user, claims.org_team_id),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
    )


async def _landing_session(
    request: Request, db: DbSession
) -> tuple[str, AuthRefreshToken, User] | JSONResponse:
    """The login session a landing action acts on, named by the refresh cookie
    exactly as a switch names it, or the refusal to answer with. Served only
    while multi-org is on."""
    if not multi_org_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    raw = request.cookies.get(settings.auth_refresh_cookie_name)
    if not raw:
        raise switch_refused(
            status.HTTP_401_UNAUTHORIZED, code="unauthorized", message="missing refresh token"
        )
    if request.headers.get(SWITCH_CSRF_HEADER) != SWITCH_CSRF_VALUE:
        raise switch_refused(
            status.HTTP_403_FORBIDDEN, code="csrf", message="This request is missing its header."
        )
    try:
        row, user = await resolve_refresh_token(db, raw)
    except RefreshError as exc:
        return await answer_refresh_error(db, request, exc)
    _, banned = await user_with_ban(db, user.id)
    if banned:
        await revoke_all_for_user(db, user.id)
        await db.commit()
        return refresh_refused(request, code="unauthorized", message="user no longer exists")
    same_identity_or_absent(request, user.id)
    return raw, row, user


def _verification_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "email_verification_required",
            "message": "Verify your email address to perform this action.",
        },
    )


@router.post(
    "/refresh/org/new",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_auth_throttle],
)
async def create_org_from_landing(
    payload: CreateOrgRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse | JSONResponse:
    """Create an org from the sign-in landing and enter it.

    For a person whose login session names no org they can enter (they left
    or were removed from every org): the session is the refresh cookie, as on
    a switch, and the org is founded exactly as ``POST /orgs`` founds one,
    under the same creation cap."""
    session = await _landing_session(request, db)
    if isinstance(session, JSONResponse):
        return session
    raw, row, user = session
    if not is_verified(user):
        raise _verification_required()
    try:
        org = await found_org(
            db, user=user, name=payload.name, now=datetime.now(UTC), client=client_hint(request)
        )
    except OrgCreationLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "org_creation_limited",
                "message": "You've created too many organizations recently. Try again later.",
            },
        ) from exc
    emit_event(EventName.org_created, user_id=user.id, org_id=org.id)
    return await _enter_from_landing(
        db,
        request,
        response,
        raw=raw,
        user=user,
        previous_access_jti=row.access_jti,
        org_id=org.id,
    )


@router.post("/refresh/org/join", response_model=LoginResponse, dependencies=[_auth_throttle])
async def join_org_from_landing(
    payload: JoinOrgRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse | JSONResponse:
    """Accept an invitation link from the sign-in landing and enter its org.

    The same acceptance the signed-in link route makes: the link is bound to
    the address it was mailed to, so a session for another account is told
    which address (masked) and nothing is created. When the org's sign-in
    policy wants its single sign-on first, the acceptance stands and the
    answer is the switch route's 409, naming the IdP."""
    session = await _landing_session(request, db)
    if isinstance(session, JSONResponse):
        return session
    raw, row, user = session
    invitation = await invitation_by_token(db, payload.token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    closed = invitation_link_closed_reason(invitation)
    if closed is not None:
        code, message = closed
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail={"code": code, "message": message}
        )
    if user.email != invitation.email.lower():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "invitation_other_account",
                "message": (
                    "This invitation is for "
                    f"{masked_invited_address(invitation.email)}. "
                    "Sign in with that email to accept."
                ),
            },
        )
    org_id = await org_root_of(db, invitation.team_id)
    try:
        await accept_invitation(
            db, invitation, user=user, actor=actor_for_user(user, org_id=org_id)
        )
    except InvitationError as exc:
        detail: str | dict[str, str] = (
            {"code": exc.code.value, "message": str(exc)} if exc.code is not None else str(exc)
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
    emit_event(EventName.invitation_accepted, user_id=user.id, org_id=org_id)
    policy = await sign_in_policy.evaluate(
        db, user=user, org_team_id=org_id, family_id=row.family_id, method=None
    )
    if isinstance(policy, sign_in_policy.StepUp):
        # The person joined; entering takes the org's own sign-in.
        await db.commit()
        step_up: dict[str, str] = {
            "code": policy.kind,
            "message": (
                "This organization requires single sign-on."
                if policy.kind == "sso_required"
                else "This organization doesn't allow this sign-in method."
            ),
        }
        if policy.login_url is not None:
            step_up["login_url"] = sso_login_with_return(
                policy.login_url, return_to=f"/?{urlencode({'switch_org': str(org_id)})}"
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=step_up)
    return await _enter_from_landing(
        db,
        request,
        response,
        raw=raw,
        user=user,
        previous_access_jti=row.access_jti,
        org_id=org_id,
    )


@router.get("/memberships", response_model=MembershipListResponse)
async def list_memberships(
    request: Request, user: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> MembershipListResponse:
    """The caller's own active memberships, most recently used first: the orgs
    this person can switch into; then, with multi-org on, the orgs that
    provisioned them and wait for them to join (``status: "pending"``). Never
    anyone else's, whoever asks.

    A browser session an org's IdP started sees only the org it is in: an
    org's admins run its IdP, and must not learn which other orgs a member
    belongs to."""
    views = await switchable_orgs(db, user)
    if not await speaks_for_account(db, request, user):
        views = [view for view in views if view.org_team_id == org_id]
    return MembershipListResponse(
        active_org_team_id=org_id,
        memberships=[
            MembershipRead(
                org_team_id=view.org_team_id,
                org_name=view.org_name,
                role=view.role,
                sso_required=view.sso_required,
                last_active_at=view.last_active_at,
                status=view.status,
            )
            for view in views
        ],
    )


@router.post("/memberships/join", response_model=MembershipRead, dependencies=[_auth_throttle])
async def join_membership(
    payload: JoinMembershipRequest,
    request: Request,
    user: CurrentUser,
    org_id: CurrentOrg,
    db: DbSession,
) -> MembershipRead:
    """Accept an org's pending membership: the org provisioned the person (its
    SCIM), and only the person can say yes. The org in the body is only a
    choice among the caller's own pending memberships; anything else, and
    every call while multi-org is off, is the same 404. The org's sign-in
    policy is asked first, against this browser's login session: a step-up is
    the 409 the switch route answers, naming the org's single sign-on, and
    nothing changes. On success the membership is active and the person may
    switch into it (``POST /auth/refresh/org``)."""
    target = payload.org_team_id
    membership = await joinable_org(db, user, target)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    jti = acting_session_jti(request)
    family = await family_of_access_token(db, jti) if jti else None
    policy = await sign_in_policy.evaluate(
        db,
        user=user,
        org_team_id=target,
        family_id=family.family_id if family is not None else None,
        method=None,
    )
    if isinstance(policy, sign_in_policy.StepUp):
        detail: dict[str, str] = {
            "code": policy.kind,
            "message": (
                "This organization requires single sign-on."
                if policy.kind == "sso_required"
                else "This organization doesn't allow this sign-in method."
            ),
        }
        if policy.login_url is not None:
            detail["login_url"] = sso_login_with_return(
                policy.login_url, return_to=f"/?{urlencode({'switch_org': str(target)})}"
            )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    # The session is held to the org the caller is signed in to; the seat
    # the join writes is in the org being joined.
    async with cross_tenant_write(db, reason="membership.join"):
        await activate_pending_membership(db, membership, actor=actor_for_user(user, org_id=target))
    await record_org_audit(
        db, org_id=target, actor=user, action="membership.joined", target=user.email
    )
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.org_joined",
        org_team_id=org_id,
        client=client_hint(request),
        detail={"joined_org_team_id": str(target)},
    )
    org = await db.get(Team, target)
    return MembershipRead(
        org_team_id=target,
        org_name=org.name if org is not None else "",
        role=await org_role_in(db, membership),
        sso_required=await org_sso_required_for(db, user, membership),
        last_active_at=membership.last_active_at,
        status="active",
    )
