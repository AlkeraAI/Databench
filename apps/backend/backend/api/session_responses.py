"""The answers the browser-session routes share.

Every route that rotates, switches or refuses the login session (sign-in,
refresh, the org switch, the sign-in landing of a person with no org)
answers through these, so a refusal clears the same cookies and carries the
same envelope whichever route gave it.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.auth import (
    COOKIE_NAME,
    RefreshError,
    RefreshReuseError,
    SessionClaims,
    decode_session_token,
    sign_in_policy,
)
from alkera_core.models import Team, User
from alkera_core.observability.context import get_trace_id, new_trace_id
from alkera_core.observability.envelope import build_error_body
from alkera_core.schemas.identity.user import UserRead
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError

from backend.auth.dependencies import (
    DbSession,
)
from backend.auth.session_issue import client_hint
from backend.services.audit import record_security_event
from backend.services.identity import (
    org_membership_count,
    org_role_in,
    verification_resend_available_at,
)
from backend.services.org import (
    membership_in,
    teams_administered_by,
)
from backend.utils.cookies import clear_refresh_cookie, clear_session_cookie, set_refresh_cookie

#: The header a switch must carry. A cross-site form cannot set it, and a
#: cross-site ``fetch`` that tries is preflighted and refused by CORS; with the
#: refresh cookie's ``SameSite=Strict`` it is the second of two locks.
SWITCH_CSRF_HEADER = "X-Requested-With"
SWITCH_CSRF_VALUE = "alkera"


def clear_session_cookies(response: Response) -> None:
    clear_session_cookie(response)
    clear_refresh_cookie(response)


def refresh_refused(request: Request, *, code: str, message: str) -> JSONResponse:
    """A refresh refusal in the platform's error envelope, carrying the
    cookie clears — built as a response rather than raised, because headers
    set on the injected ``Response`` do not survive an ``HTTPException``."""
    trace_id = getattr(request.state, "trace_id", None) or get_trace_id() or new_trace_id()
    resp = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=build_error_body(
            code=code, message=message, trace_id=trace_id, status=status.HTTP_401_UNAUTHORIZED
        ),
        headers={"WWW-Authenticate": "Cookie"},
    )
    clear_session_cookies(resp)
    return resp


async def answer_refresh_error(db: DbSession, request: Request, exc: RefreshError) -> JSONResponse:
    """Answer a refused rotation. The refusal's own writes (a family ended on
    reuse or expiry) are committed so they outlive the 401, and a reuse is
    recorded on the identity's security log: someone held a copy of the
    session's refresh token."""
    if isinstance(exc, RefreshReuseError):
        await record_security_event(
            db,
            user_id=exc.user_id,
            event="auth.refresh_token_reused",
            client=client_hint(request),
            detail={"family_id": str(exc.family_id)},
        )
    await db.commit()
    return refresh_refused(request, code=exc.code, message=str(exc))


def step_up_refused(
    request: Request, *, policy: sign_in_policy.StepUp, refresh_raw: str
) -> JSONResponse:
    """A refresh the family's org refused until the person signs in again its
    way. The refresh cookie is renewed (the family stands, so an SSO sign-in
    can step it up) and the session cookie is left in place: its access token
    is revoked and serves only to name the family to the SSO callback."""
    trace_id = getattr(request.state, "trace_id", None) or get_trace_id() or new_trace_id()
    resp = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=build_error_body(
            code=policy.kind,
            message=(
                "Your organization requires single sign-on."
                if policy.kind == "sso_required"
                else "Your organization doesn't allow this sign-in method."
            ),
            trace_id=trace_id,
            status=status.HTTP_401_UNAUTHORIZED,
            details={"login_url": policy.login_url} if policy.login_url else None,
        ),
        headers={"WWW-Authenticate": "Cookie"},
    )
    set_refresh_cookie(resp, refresh_raw)
    return resp


def switch_refused(status_code: int, *, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def same_identity_or_absent(request: Request, user_id: UUID) -> SessionClaims | None:
    """The access cookie's claims when it names ``user_id``, None when there is
    no access cookie. A cookie for another identity, or one that does not
    decode, is a 401: this browser's two cookies disagree about who it is."""
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        claims = decode_session_token(raw, allow_expired=True)
    except InvalidTokenError:
        claims = None
    if claims is None or claims.user_id != user_id:
        raise switch_refused(
            status.HTTP_401_UNAUTHORIZED, code="unauthorized", message="session mismatch"
        )
    return claims


async def user_read(db: DbSession, user: User, org_id: UUID) -> UserRead:
    """Build a UserRead for the org the session is in (``org_id``), with that
    org's name filled in. The org name is the signal
    the SPA reads to decide whether to prompt for it on complete-profile (empty
    string = an unnamed minimal-signup org), so the endpoints that drive that gate
    go through here instead of a bare `UserRead.model_validate`.

    The teams this account administers ride along for the same reason: a client
    that offers a team picker has to know which teams are selectable, and the
    answer is one query here rather than a probe per team. It is filled on every
    read that returns the current user, including the ones a client seeds its
    cache from after a login, so the set is never briefly empty for a person who
    does administer something."""
    read = UserRead.model_validate(user)
    read.org_team_id = org_id
    org = await db.get(Team, org_id)
    read.org_name = org.name if org is not None else ""
    read.verification_resend_available_at = verification_resend_available_at(user)
    read.admin_team_ids = (
        sorted(await teams_administered_by(db, user.id, org_team_id=org.id))
        if org is not None
        else []
    )
    membership = await membership_in(db, user_id=user.id, org_team_id=org_id)
    read.org_role = await org_role_in(db, membership) if membership is not None else "member"
    read.membership_count = max(1, await org_membership_count(db, user))
    return read
