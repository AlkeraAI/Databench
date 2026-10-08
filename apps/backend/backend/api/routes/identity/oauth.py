"""OAuth / federated-login routes.

Flow (see `backend/services/identity/oauth.py` for the decision tree):

    GET  /{provider}/start      → set signed state cookie, redirect to provider
    GET  /{provider}/callback   → exchange code, resolve → login OR register ticket
    GET  /register/context      → prefill data for the SPA register screen
    POST /register              → finish an OAuth-initiated registration → session
    GET  /providers             → which providers are wired up (button rendering)
    GET  /mock/authorize        → dev-only stand-in consent screen (mock provider)
"""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime
from urllib.parse import urlencode

from alkera_core.auth import (
    InvalidTokenError,
    decode_oauth_state,
    decode_register_ticket,
    encode_oauth_state,
)
from alkera_core.config import settings
from alkera_core.schemas.identity.auth import (
    LoginResponse,
    OAuthProvidersResponse,
    OAuthRegisterContext,
    OAuthRegisterRequest,
)
from alkera_core.schemas.identity.user import UserRead
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.api.params import PathId
from backend.api.rate_limit import limited
from backend.api.return_path import frontend_url, safe_return_path
from backend.auth.dependencies import DbSession
from backend.auth.email_policy import is_personal_email
from backend.auth.oauth import get_registry
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.mock import MOCK_AUTHORIZE_PATH, MockProvider
from backend.auth.oauth.profile import FederatedProfile
from backend.auth.oauth.registry import UnknownProviderError
from backend.auth.session_issue import issue_session
from backend.services.abuse import bans as ban_service
from backend.services.identity import (
    LoginOutcome,
    OAuthLoginBlockedError,
    OAuthRegistrationError,
    RegisterOutcome,
    account_exists_detail,
    run_signup_hooks,
)
from backend.services.identity import oauth as oauth_service
from backend.services.identity.oauth import OAuthAccountExistsError
from backend.utils.cookies import (
    OAUTH_STATE_COOKIE,
    clear_oauth_state_cookie,
    set_oauth_state_cookie,
)

router = APIRouter(prefix="/api/v1/auth/oauth", tags=["oauth"])


# --- helpers ---------------------------------------------------------------


# The open-redirect guard and the app-origin resolver are shared with the
# edge-gate return (backend.api.return_path); these names keep the call sites.
_safe_return_to = safe_return_path
_frontend = frontend_url


def _login_error_redirect(reason: str) -> RedirectResponse:
    """Uniform error landing. `reason` is a non-sensitive code; it never reveals
    whether an account exists or which credential failed."""
    resp = RedirectResponse(_frontend(f"/login?oauth_error={reason}"), status_code=302)
    clear_oauth_state_cookie(resp)
    return resp


def _callback_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base}/api/v1/auth/oauth/{provider}/callback"


# --- discovery -------------------------------------------------------------


@router.get("/providers", response_model=OAuthProvidersResponse)
async def list_providers() -> OAuthProvidersResponse:
    """Platform-configured provider keys, for rendering sign-in buttons."""
    registry = get_registry()
    return OAuthProvidersResponse(providers=registry.keys())


# --- handshake -------------------------------------------------------------


@router.get("/{provider}/start", dependencies=[Depends(limited("credential"))])
async def start(
    provider: PathId,
    intent: str = Query(default="login"),
    invite_token: str | None = Query(default=None),
    return_to: str | None = Query(default=None),
) -> Response:
    registry = get_registry()
    try:
        impl = registry.get(provider)
    except UnknownProviderError:
        return _login_error_redirect("unknown_provider")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    tx = encode_oauth_state(
        state=state,
        nonce=nonce,
        provider=provider,
        intent=intent,
        invite_token=invite_token,
        return_to=_safe_return_to(return_to),
    )
    try:
        url = await impl.authorization_url(
            redirect_uri=_callback_uri(provider), state=state, nonce=nonce
        )
    except OAuthError:
        return _login_error_redirect("provider_unavailable")

    resp = RedirectResponse(url, status_code=302)
    set_oauth_state_cookie(resp, tx)
    return resp


@router.get("/{provider}/callback", dependencies=[Depends(limited("credential"))])
async def callback(
    provider: PathId,
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
) -> Response:
    raw_tx = request.cookies.get(OAUTH_STATE_COOKIE)
    if not raw_tx:
        return _login_error_redirect("expired")
    try:
        tx = decode_oauth_state(raw_tx)
    except InvalidTokenError:
        return _login_error_redirect("expired")

    # CSRF: the returned state must match the signed one, for this provider.
    if not code or not state or state != tx.state or provider != tx.provider:
        return _login_error_redirect("state")

    registry = get_registry()
    try:
        impl = registry.get(provider)
    except UnknownProviderError:
        return _login_error_redirect("unknown_provider")

    try:
        profile = await impl.fetch_profile(
            code=code, redirect_uri=_callback_uri(provider), nonce=tx.nonce
        )
    except OAuthError:
        return _login_error_redirect("exchange")

    try:
        outcome = await oauth_service.resolve(db, profile, invite_token=tx.invite_token)
    except OAuthLoginBlockedError as exc:
        return _login_error_redirect(exc.reason)

    if isinstance(outcome, RegisterOutcome):
        params = {"oauth_ticket": outcome.ticket, "return_to": tx.return_to or "/dashboard"}
        # A brand-new user who authed with a personal account lands on the
        # dedicated "use your work email" page (which can re-run OAuth or, via a
        # small link, continue to the normal register form anyway). Business
        # emails go straight to the register form. Returning logins (LoginOutcome)
        # are never gated.
        if is_personal_email(profile.email):
            target = "/oauth/business-email"
        else:
            target = "/signup"
        resp = RedirectResponse(_frontend(f"{target}?{urlencode(params)}"), status_code=302)
        clear_oauth_state_cookie(resp)
        return resp

    assert isinstance(outcome, LoginOutcome)
    user = outcome.user
    # Profile-completeness gate (JIT/SSO safety net; self-serve users have names).
    if user.profile_complete:
        target = tx.return_to or "/dashboard"
    else:
        target = f"/complete-profile?{urlencode({'return_to': tx.return_to or '/dashboard'})}"
    if outcome.choose_org:
        # The org the person used last needs a step-up first: the session
        # landed in another org, and the chooser lets them pick.
        target = f"/choose-org?{urlencode({'return_to': tx.return_to or '/dashboard'})}"

    resp = RedirectResponse(_frontend(target), status_code=302)
    await issue_session(
        db, user, request=request, response=resp, method=provider, org_team_id=outcome.org_team_id
    )
    clear_oauth_state_cookie(resp)
    return resp


# --- registration completion ----------------------------------------------


@router.get("/register/context", response_model=OAuthRegisterContext)
async def register_context(ticket: str = Query(...)) -> OAuthRegisterContext:
    try:
        decoded = decode_register_ticket(ticket)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired ticket"
        ) from exc
    return OAuthRegisterContext(
        provider=decoded.provider,
        email=decoded.email,
        first_name=decoded.first_name,
        last_name=decoded.last_name,
        has_invite=decoded.invite_token is not None,
    )


@router.post(
    "/register",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limited("credential"))],
)
async def register(
    payload: OAuthRegisterRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    try:
        ticket = decode_register_ticket(payload.oauth_ticket)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired ticket"
        ) from exc

    # A banned address (by account or by domain) is answered as the route
    # answers a ticket it cannot use, so the ban is not told apart from expiry.
    if await ban_service.email_is_banned(db, ticket.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired ticket"
        )

    try:
        user = await oauth_service.complete_registration(
            db,
            ticket,
            first_name=payload.first_name,
            last_name=payload.last_name,
            org_name=payload.org_name,
            invite_token=payload.invite_token,
            allow_personal_email=payload.allow_personal_email,
        )
    except OAuthLoginBlockedError as exc:
        # `reason` is a stable machine string (e.g. "personal_email") the SPA
        # branches on; carry it as the envelope `error.code`.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.reason, "message": "Sign-in could not be completed."},
        ) from exc
    except OAuthAccountExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=account_exists_detail(exc.invite_token),
        ) from exc
    except OAuthRegistrationError as exc:
        msg = str(exc)
        code = status.HTTP_409_CONFLICT if "already exists" in msg else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=msg) from exc

    # The same signup hooks the email/password /signup route runs.
    await run_signup_hooks(request.cookies, db, user)

    claims = await issue_session(
        db, user, request=request, response=response, method=ticket.provider
    )
    return LoginResponse(
        user=UserRead.model_validate(user),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
    )


# --- dev-only mock consent screen -----------------------------------------


@router.get("/mock/authorize", response_class=HTMLResponse)
async def mock_authorize(
    state: str = Query(...),
    redirect_uri: str = Query(...),
    email: str | None = Query(default=None),
    first_name: str = Query(default="Mock"),
    last_name: str = Query(default="User"),
    subject: str | None = Query(default=None),
    email_verified: bool = Query(default=True),
    submit: str | None = Query(default=None),
) -> Response:
    """A stand-in for a real provider's consent screen.

    Only mounted when the mock provider is registered (dev/test). Lets a human
    type any identity and bounce back through the real callback — no creds.
    """
    if not get_registry().has("mock"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Confine the bounce target to our own callback — the mock must never be an
    # open redirect, even in dev.
    if not redirect_uri.startswith(f"{settings.oauth_redirect_base}/api/v1/auth/oauth/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid redirect URL")

    if submit and email:
        profile = FederatedProfile(
            provider="mock",
            subject=subject or email,
            email=email,
            email_verified=email_verified,
            first_name=first_name,
            last_name=last_name,
        )
        params = {"state": state, "code": MockProvider.encode_code(profile)}
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    # Reflected params are escaped (defense in depth; this page is dev-only).
    safe_state = html.escape(state, quote=True)
    safe_redirect = html.escape(redirect_uri, quote=True)
    form = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"><title>Mock sign-in</title>
  <style>
    body {{ font-family: system-ui; max-width: 28rem; margin: 4rem auto; }}
    input[type=text], input[type=email] {{ width: 100%; }}
  </style>
</head>
<body>
  <h1>Mock provider sign-in</h1>
  <p style="color:#666">Dev-only. Simulates Google/GitHub for local testing.</p>
  <form method="get" action="{MOCK_AUTHORIZE_PATH}">
    <input type="hidden" name="state" value="{safe_state}">
    <input type="hidden" name="redirect_uri" value="{safe_redirect}">
    <p><label>Email<br><input name="email" type="email" value="dev@example.com"></label></p>
    <p><label>First name<br><input name="first_name" type="text" value="Dev"></label></p>
    <p><label>Last name<br><input name="last_name" type="text" value="User"></label></p>
    <p><label>Subject (stable id)<br>
      <input name="subject" type="text" placeholder="(defaults to email)"></label></p>
    <p><label>
      <input type="checkbox" name="email_verified" value="true" checked> Email verified
    </label></p>
    <button type="submit" name="submit" value="1">Continue</button>
  </form>
</body>
</html>"""
    return HTMLResponse(form)


__all__ = ["router"]
