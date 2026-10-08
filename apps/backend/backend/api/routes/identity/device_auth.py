"""Device Authorization Grant routes (RFC 8628).

Two wire-shape families share this router:

- The CLI-facing endpoints (`POST /device/code`, `POST /device/token`) follow
  RFC 8628 exactly: **form-encoded** input and **flat** JSON output
  (`{"device_code": ...}` / `{"error": "authorization_pending"}`), with
  `Cache-Control: no-store`. They MUST NOT raise `HTTPException` for their error
  states — the global handler would rewrap them into the house
  `{"error": {"code", "message"}}` envelope and corrupt the spec wire shape. They
  return `JSONResponse` directly via `_rfc_error`.

- The SPA-facing endpoints (`GET /device/info`, `POST /device/approve`,
  `POST /device/deny`) are JSON endpoints using Pydantic + the house error
  envelope, like the rest of the API. They take a browser session only
  (`require_browser_session`): approving a device mints a new long-lived
  token, which a CLI, CI, proxy, personal access or machine credential must
  never be able to do for itself.

Approval records an org, and redemption mints the CLI token for that org
through ``mint_for_membership``: the CLI acts in that org, and only while the
person's membership there stands. The org is the one the approving browser
session is in, unless the approver picks another of their own orgs
(``org_team_id`` on the approve body, which ``alkera login --org`` pre-selects
through ``/device?org=``): it must be one of their active memberships and admit
their session under its sign-in policy.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from alkera_core.auth import register_token, sign_in_policy
from alkera_core.auth.machine_credential_standing import owner_stands
from alkera_core.auth.refresh import family_of_access_token
from alkera_core.auth.tenancy import MembershipRefused, home_org_id
from alkera_core.config import settings
from alkera_core.models import DeviceAuthorization, TokenType, User
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.identity.auth import (
    DeviceApprovalRequest,
    DeviceApproveRequest,
    DeviceInfoResponse,
    MessageResponse,
)
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse

from backend.api.rate_limit import limited
from backend.api.return_path import sso_login_with_return
from backend.auth.dependencies import BrowserSessionUser, CurrentOrg, DbSession
from backend.auth.membership_tokens import mint_for_membership
from backend.auth.session_issue import acting_session_jti, client_hint
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.credentials import mint_personal
from backend.services.identity import DeviceAuthError, RedeemError, enterable_org
from backend.services.identity import device_authorization as device_service

router = APIRouter(prefix="/api/v1/auth/device", tags=["device-auth"])

#: The device grant is unauthenticated by construction (there is no user yet),
#: which makes it the CLI-side twin of the password routes: `/code` mints device
#: codes and `/token` is polled until one is approved. Minting, approving and
#: denying are credential attempts. The poll is not: it repeats at the interval
#: this server issued for as long as the code lives, so it has a class sized
#: for that cadence, keyed by the device code it names.
_device_throttle = Depends(limited("credential"))
_device_poll_throttle = Depends(limited("device_poll"))

_DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _rfc_error(
    error: str,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    *,
    description: str | None = None,
) -> JSONResponse:
    """An RFC 8628 §3.5-shaped error: flat ``{"error": "..."}``, no-store,
    with the optional RFC 6749 ``error_description``.

    Bypasses the house error envelope on purpose (see module docstring)."""
    body = {"error": error}
    if description is not None:
        body["error_description"] = description
    return JSONResponse(body, status_code=status_code, headers=_NO_STORE)


@router.post("/code", dependencies=[_device_throttle])
async def device_code(
    db: DbSession,
    # Optional at the FastAPI layer so an omitted field returns the flat RFC
    # `invalid_request` shape instead of FastAPI's house-envelope 422.
    client_id: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Device authorization endpoint (public). Mint a device_code + user_code."""
    if client_id is None:
        return _rfc_error("invalid_request")
    # RFC 6749 §5.2 `invalid_client`: this endpoint is unauthenticated, so the
    # client_id is the only thing naming the requester on the consent screen.
    # Anything outside the shipped clients is refused rather than rendered as the
    # generic "Alkera Client" label.
    if not device_service.is_known_client(client_id):
        return _rfc_error("invalid_client")
    raw_device_code, row = await device_service.create_device_code(
        db, client_id=client_id, scope=scope
    )
    verification_uri = f"{settings.frontend_base_url.rstrip('/')}/device"
    body = {
        "device_code": raw_device_code,
        "user_code": row.user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?user_code={row.user_code}",
        "expires_in": settings.auth_device_code_ttl_seconds,
        "interval": row.interval_seconds,
    }
    return JSONResponse(body, status_code=status.HTTP_200_OK, headers=_NO_STORE)


@router.post("/token", dependencies=[_device_poll_throttle])
async def device_token(
    db: DbSession,
    request: Request,
    # Optional at the FastAPI layer so an omitted field returns the flat RFC
    # `invalid_request` shape instead of FastAPI's house-envelope 422.
    grant_type: Annotated[str | None, Form()] = None,
    device_code: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Token endpoint, device grant (public). Polled by the CLI/daemon.

    Returns the access token once the user approves; otherwise an RFC 8628 §3.5
    error (`authorization_pending` / `slow_down` / `access_denied` /
    `expired_token` / `invalid_grant`)."""
    if grant_type is None or device_code is None or client_id is None:
        return _rfc_error("invalid_request")
    if grant_type != _DEVICE_CODE_GRANT_TYPE:
        return _rfc_error("unsupported_grant_type")

    result = await device_service.redeem(db, raw_device_code=device_code, client_id=client_id)
    if isinstance(result, RedeemError):
        return _rfc_error(result.value)

    user, row = result
    # The org the approving browser session was in; a row approved before
    # grants recorded one is the home org.
    org_id = row.org_team_id or home_org_id(user)
    if client_id == device_service.PERSONAL_BOX_CLIENT_ID:
        return await _personal_box_credential(db, user, org_id=org_id, request=request)
    try:
        token, claims = await mint_for_membership(db, user, org_id, kind="cli")
    except MembershipRefused as exc:
        # `access_denied` is the RFC code every client already stops on; the
        # description says which refusal it was. The grant stays consumed.
        return _rfc_error("access_denied", description=exc.code)
    await register_token(db, claims=claims, token_type=TokenType.CLI)
    await _record_device_sign_in(db, user, org_id=org_id, client_id=client_id, request=request)
    # The device grant never passes /auth/login, so redemption writes the
    # audit row itself, in the org the token was minted for.
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=user,
        action="auth.device_login",
        target=user.email,
        detail={"client_id": client_id},
    )
    emit_event(EventName.cli_token_minted, user_id=user.id, org_id=org_id)
    body = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": claims.expires_at - claims.issued_at,
    }
    return JSONResponse(body, status_code=status.HTTP_200_OK, headers=_NO_STORE)


async def _lookup_pending_or_404(
    db: DbSession, user: BrowserSessionUser, user_code: str
) -> DeviceAuthorization:
    """Resolve a pending authorization for the SPA, enforcing the per-session
    brute-force budget. Missing/expired/resolved codes return an indistinguishable
    404 (no enumeration oracle)."""
    if not device_service.assert_user_code_attempts(user.id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "Too many attempts; try again later"},
        )
    row = await device_service.get_for_approval(db, user_code)
    if row is None:
        device_service.record_user_code_failure(user.id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device code not found or expired"
        )
    return row


@router.get("/info", response_model=DeviceInfoResponse)
async def device_info(
    user: BrowserSessionUser, db: DbSession, user_code: str
) -> DeviceInfoResponse:
    """Consent-screen data for a pending authorization (cookie-authed)."""
    row = await _lookup_pending_or_404(db, user, user_code)
    return DeviceInfoResponse(
        client_id=row.client_id,
        client_name=device_service.client_name_for(row.client_id),
        scope=row.scope,
        expires_at=row.expires_at,
        user_code=row.user_code,
    )


@router.post("/approve", response_model=MessageResponse, dependencies=[_device_throttle])
async def device_approve(
    payload: DeviceApproveRequest,
    request: Request,
    user: BrowserSessionUser,
    org_id: CurrentOrg,
    db: DbSession,
) -> MessageResponse:
    """Approve a pending authorization, binding it to the authenticated user and
    to one of their orgs: the one their browser session is in, or the one the
    body names.

    A named org must be one of the approver's active memberships; anything else
    is the same 404, so the route never says whether an org exists. The org's
    sign-in policy is then asked about the approving session: a CLI token for
    an org that requires SSO is only ever born under a fresh sign-in through
    that org's IdP."""
    row = await _lookup_pending_or_404(db, user, payload.user_code)
    target = payload.org_team_id or org_id
    if target != org_id and await enterable_org(db, user, target) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    jti = acting_session_jti(request)
    family = await family_of_access_token(db, jti) if jti is not None else None
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
                "Sign in with your organization's single sign-on, then approve again."
                if policy.kind == "sso_required"
                else "Your organization doesn't allow this sign-in method."
            ),
        }
        if policy.login_url is not None:
            back = urlencode({"user_code": row.user_code, "org": str(target)})
            detail["login_url"] = sso_login_with_return(
                policy.login_url, return_to=f"/device?{back}"
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    try:
        await device_service.approve(db, row, user=user, org_team_id=target)
    except DeviceAuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return MessageResponse(message="Device approved")


@router.post("/deny", response_model=MessageResponse, dependencies=[_device_throttle])
async def device_deny(
    payload: DeviceApprovalRequest, user: BrowserSessionUser, db: DbSession
) -> MessageResponse:
    """Deny a pending authorization."""
    row = await _lookup_pending_or_404(db, user, payload.user_code)
    try:
        await device_service.deny(db, row)
    except DeviceAuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return MessageResponse(message="Device denied")


__all__ = ["router"]


async def _record_device_sign_in(
    db: DbSession, user: User, *, org_id: UUID, client_id: str, request: Request
) -> None:
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.signed_in",
        org_team_id=org_id,
        client=client_hint(request),
        detail={"method": "device", "client_id": client_id},
    )


async def _personal_box_credential(
    db: DbSession, user: User, *, org_id: UUID, request: Request
) -> JSONResponse:
    """The approver's own box gets a machine credential, never a session: bound
    to the approver's org, serving only the approver's own private chats, and
    standing only while the approver remains an active member of that org. No
    admin is needed because it can only ever run the approver's own chats; no
    grant admits it because nothing is billed for a person's own hardware."""
    if not await owner_stands(db, user_id=user.id, org_id=org_id):
        return _rfc_error(RedeemError.ACCESS_DENIED.value)
    credential, raw = await mint_personal(
        db, owner=user, org_id=org_id, label=f"Personal box of {user.email}"
    )
    await _record_device_sign_in(
        db, user, org_id=org_id, client_id=device_service.PERSONAL_BOX_CLIENT_ID, request=request
    )
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=user,
        action="compute.personal_box_registered",
        target=str(credential.id),
        detail={"client_id": device_service.PERSONAL_BOX_CLIENT_ID},
    )
    body = {
        "access_token": raw,
        "token_type": "Bearer",
        "credential_type": "machine",
        "org_id": str(org_id),
    }
    return JSONResponse(body, status_code=status.HTTP_200_OK, headers=_NO_STORE)
