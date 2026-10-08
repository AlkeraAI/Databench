"""Auth routes: login, logout, me, signup."""

from __future__ import annotations

import contextlib
import math
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from ipaddress import ip_address
from uuid import UUID

from alkera_core.abuse import is_disposable_email
from alkera_core.auth import (
    COOKIE_NAME,
    RefreshError,
    family_of_access_token,
    list_active_tokens,
    list_live_families,
    mint_refresh_token,
    revoke_all_for_user,
    revoke_family_for_user,
    revoke_family_of_access_token,
    revoke_jti,
    revoke_jti_for_user,
    rotate_refresh_token,
    sign_in_policy,
)
from alkera_core.auth.refresh import (
    REASON_LOGOUT,
    REASON_USER,
    active_org_of,
    is_family_of,
    refuse_family_org,
)
from alkera_core.auth.tenancy import MembershipRefused, home_org_id, multi_org_enabled
from alkera_core.config import settings
from alkera_core.email import send_email_verification, send_in_background, send_password_reset
from alkera_core.events import actor_for_user
from alkera_core.logging import get_logger
from alkera_core.models import OAuthIdentity, Team, TokenType, User
from alkera_core.observability.context import get_trace_id, new_trace_id
from alkera_core.observability.envelope import build_error_body
from alkera_core.observability.errors import EmailSendFailedError
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.identity.auth import (
    CompleteProfileRequest,
    LoginRequest,
    LoginResponse,
    MeRead,
    MessageResponse,
    MfaCodeRequest,
    MfaConfirmResponse,
    MfaEnrollResponse,
    MfaStatusResponse,
    SessionListResponse,
    SessionRead,
)
from alkera_core.schemas.identity.user import LinkedIdentity, LinkedIdentityList, UserRead
from alkera_core.utils.email import email_domain
from alkera_core.validation.display_name import BlankableDisplayNameStr, OptionalDisplayNameStr
from alkera_core.validation.password import USER_PASSWORD_SCHEMA
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from backend.api.params import PathId
from backend.api.rate_limit import limited
from backend.api.return_path import frontend_url, safe_return_path
from backend.api.session_responses import (
    answer_refresh_error,
    clear_session_cookies,
    refresh_refused,
    step_up_refused,
    user_read,
)
from backend.auth.account_authority import (
    account_sign_in_required,
    account_standing,
    account_wide_controls_guarded,
    require_account_standing,
    speaks_for_account,
)
from backend.auth.captcha import verify_turnstile
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentUser,
    DbSession,
    optional_session_claims,
    user_is_org_admin,
)
from backend.auth.email_policy import is_personal_email
from backend.auth.password import hash_password, verify_password
from backend.auth.password_policy import enforce_password
from backend.auth.session_issue import (
    METHOD_PASSWORD,
    METHOD_RENEWAL,
    METHOD_SIGNUP,
    acting_session_jti,
    client_hint,
    issue_rotated_session,
    issue_session,
    record_grant,
    reissue_session,
)
from backend.services.abuse import bans as ban_service
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.chats import spares as spare_service
from backend.services.identity import (
    MfaError,
    PasswordResetError,
    UserConflictError,
    VerificationError,
    account_exists_detail,
    org_landing,
    run_signup_hooks,
)
from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import lockout as lockout_service
from backend.services.identity import mfa as mfa_service
from backend.services.identity import password_reset as password_reset_service
from backend.services.identity import users as user_service
from backend.services.identity import welcome as welcome_service
from backend.services.org import (
    InvitationError,
    membership_in,
)
from backend.services.org import invitations as invitation_service
from backend.services.org import teams as team_service
from backend.utils.cookies import clear_session_cookie, set_refresh_cookie

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = get_logger(__name__)

#: ONE class shared by every credential-facing auth route. They are the same
#: abuse surface — guess a password, farm accounts, flood a mailbox — so a burst
#: spent guessing must not leave signup untouched. Keyed by the account the
#: attempt names AND by the caller address, so it never gates a legitimate user
#: behind a stranger's spray from elsewhere; the per-account lockout and the
#: per-address email cooldowns still apply on top, each answering a different
#: question.
_auth_throttle = Depends(limited("credential"))

#: Minimum gap between two outbound emails for the same account on the
#: self-service password-link routes. Without it one session (or one public
#: reset request) can flood a mailbox and burn the deployment's shared sending
#: quota, which is account-wide on SES and therefore hits every tenant. Derived
#: from the pending token's expiry, so no extra column is needed. (The
#: verification-resend route has its own, longer window —
#: ``settings.email_verification_resend_cooldown_seconds`` — because it is the
#: one surface an abuser can drive from a throwaway signup loop.)
_EMAIL_RESEND_COOLDOWN = timedelta(seconds=60)

#: How recently the caller must have authenticated for the session alone to count
#: as proof when turning MFA on. Short enough that a credential lifted long after
#: the fact — a copied 90-day CLI bearer, a browser left signed in on a shared
#: machine — cannot spend it; long enough that finishing a sign-in and then
#: opening the security settings is one uninterrupted flow.
_MFA_ENROLL_AUTH_FRESHNESS = timedelta(minutes=15)


def _issued_within_cooldown(expires_at: datetime | None, *, ttl: timedelta) -> bool:
    """True when the pending token was issued less than the cooldown ago.

    Both token services stamp expiry as ``now + ttl``, so ``expires_at - ttl`` is
    the moment the current token (and its email) was issued."""
    if expires_at is None:
        return False
    return (expires_at - ttl) > datetime.now(UTC) - _EMAIL_RESEND_COOLDOWN


def _resend_too_soon(*, retry_after: timedelta | None = None) -> HTTPException:
    headers: dict[str, str] | None = None
    if retry_after is not None:
        headers = {"Retry-After": str(max(1, math.ceil(retry_after.total_seconds())))}
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "rate_limited",
            "message": "An email was just sent. Check your inbox, then try again shortly.",
        },
        headers=headers,
    )


@lru_cache(maxsize=1)
def _decoy_password_hash() -> str:
    """A throwaway argon2 hash used to equalize the cost of a login for an
    address that has no account (or no local password) with one that does.

    Without it the KDF — tens of milliseconds of deliberate work — runs only when
    the email resolves to a row with a password, which turns response latency
    into an account-existence oracle. Built from fresh randomness so no input can
    ever match it, and memoized so the cost is paid once per process."""
    return hash_password(secrets.token_urlsafe(32))


async def _record_login_failure(db: DbSession, user: User, request: Request) -> None:
    """Best-effort failed-sign-in record on the REQUEST's session, in the
    targeted identity's own security log: a wrong password happened to the
    identity, not inside any org.

    Written inside a SAVEPOINT so a failure to record can neither raise into
    the auth path nor poison the transaction carrying the lockout counter. The
    caller commits (`_commit_auth_accounting`) — the request itself rolls back
    on the 4xx that follows."""
    try:
        async with db.begin_nested():
            await record_security_event(
                db, user_id=user.id, event="auth.login_failed", client=client_hint(request)
            )
    except Exception:  # pragma: no cover - audit must never break the auth path
        log.warning("audit.auth_failure_record_failed", exc_info=True)


async def _audit_step_up(db: DbSession, user: User, org_id: UUID, kind: str) -> None:
    """Best-effort record of an org's sign-in policy refusing an identity, in
    THAT org's chain (the refusal is the org's rule), on the same savepoint
    terms as :func:`_record_login_failure`."""
    try:
        async with db.begin_nested():
            await org_audit_service.record(
                db, org_id=org_id, actor=None, action=f"auth.{kind}", target=user.email
            )
    except Exception:  # pragma: no cover - audit must never break the auth path
        log.warning("audit.step_up_record_failed", action=kind, exc_info=True)


def _account_deactivated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "account_deactivated", "message": "This account is deactivated."},
    )


async def _commit_auth_accounting(db: DbSession) -> None:
    """Persist the security accounting (lockout counter + audit row) written for
    a refused authentication, before raising the 4xx that rolls the request back.

    Deliberately reuses the request's own session: opening a second pooled
    session while this one still holds a connection lets a handful of concurrent
    anonymous failures deadlock the whole connection pool. Never raises —
    accounting must not turn a 401 into a 500."""
    try:
        await db.commit()
    except Exception:  # pragma: no cover - accounting must never break the auth path
        log.warning("auth.accounting_commit_failed", exc_info=True)
        with contextlib.suppress(Exception):
            await db.rollback()


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP for Turnstile's optional `remoteip`. Prefers the
    first X-Forwarded-For hop (the backend runs behind nginx/an ALB in prod),
    falling back to the socket peer. Spoofable, so it's a hint, not a gate."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _forensics_ip(request: Request) -> str | None:
    """The client IP as attested by OUR proxy chain, validated for persistence.

    Unlike the Turnstile hint above, this must resist forgery: a caller can
    invent any LEFT-side X-Forwarded-For hops, but each trusted reverse proxy
    APPENDS the peer it actually saw — so the address
    ``forwarded_for_trusted_hops`` from the RIGHT is proxy-attested (the socket
    peer when nothing is in front). Anything unparseable (garbage, an
    over-long value past the column's 45 chars, an injection probe) yields
    NULL instead of failing the signup/login it rides on."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        trusted = max(1, settings.forwarded_for_trusted_hops)
        # A header with FEWER hops than our chain appends means the chain was
        # bypassed (or misconfigured) — every remaining entry is then
        # caller-influenced, so nothing in it is attestable. Record nothing
        # rather than fall back to a forgeable hop.
        raw = hops[-trusted] if len(hops) >= trusted else None
    else:
        # No header at all: the socket peer is the connection's own source —
        # not forgeable, and the correct answer when nothing fronts the app.
        raw = request.client.host if request.client else None
    if not raw:
        return None
    try:
        return str(ip_address(raw))
    except ValueError:
        return None


class SignupRequest(BaseModel):
    """Minimal signup: only email + password are required. Name and org are
    finished on the complete-profile step.

    Org selection (mutually exclusive — never both):
    - `invite_token` set → join the inviter's org with the invitation's role.
    - `org_name` set → create a new org with that name (the caller becomes Org
      Admin). Optional: omitting it creates a new *unnamed* org the caller names
      on complete-profile.

    Names are optional (the empty-string "profile incomplete" sentinel) and
    collected on complete-profile; a caller that already knows them (apps/web,
    OAuth) may still send them here.
    """

    email: EmailStr
    # The names default to the empty "profile incomplete" sentinel, so they are
    # BLANKABLE display names: "" passes through as the sentinel, and anything
    # actually typed is held to the link/markup policy the invitation email
    # depends on.
    first_name: BlankableDisplayNameStr = Field(default="", max_length=255)
    last_name: BlankableDisplayNameStr = Field(default="", max_length=255)
    password: str = Field(min_length=8, max_length=255, json_schema_extra=USER_PASSWORD_SCHEMA)
    org_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)
    invite_token: str | None = None
    # Escape hatch for the business-email gate: the SPA sets this only after the
    # user explicitly clicks "continue with my personal email anyway".
    allow_personal_email: bool = False
    # Cloudflare Turnstile token from the SPA's invisible captcha (verified before
    # account creation when Turnstile is configured; ignored otherwise).
    turnstile_token: str | None = None


@router.post("/login", response_model=LoginResponse, dependencies=[_auth_throttle])
async def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse | JSONResponse:
    await verify_turnstile(payload.turnstile_token, remote_ip=_client_ip(request))
    user, banned = await user_service.get_by_email_with_ban(db, payload.email)
    # Which counter governs this attempt. A banned account is answered exactly
    # as an unknown address is — including its lockout — because to the caller
    # it must not exist, and a counter of its own would say it does.
    known = user is not None and not banned
    # Brute-force lockout — checked BEFORE the password so a locked account can't
    # even probe (and a wrong password adds to the counter via record_failure).
    # An address with no usable account keeps its counter in `login_lockouts`
    # instead, so the 429 arrives on the same attempt either way. BOTH reads are
    # issued whichever branch will be believed: a lookup that happened only for
    # addresses that turned out not to exist would make a refusal's cost depend
    # on existence, which is the timing oracle the decoy KDF below exists to
    # close.
    unknown_expiry = await lockout_service.unknown_lock_expiry(db, payload.email)
    expiry = (
        lockout_service.lock_expiry(user) if user is not None and not banned else unknown_expiry
    )
    if expiry is not None:
        raise lockout_service.locked_out(expiry)
    # Always run the KDF exactly once — on a decoy hash for an unknown address or
    # an account with no local password — so response latency doesn't disclose
    # which addresses have accounts.
    # A banned account runs the KDF on its real hash and is then refused as an
    # unknown address is — the same response, after the same work.
    stored_hash = user.password_hash if user is not None else None
    password_ok = verify_password(payload.password, stored_hash or _decoy_password_hash())
    if user is None or banned or not password_ok:
        # Audit + count the failure, then commit — this request rolls back on the
        # 401 below, but the brute-force signal must persist. An address with no
        # usable account is counted in `login_lockouts` on the same schedule, so
        # the 429 arrives at the same attempt either way.
        if known:
            await lockout_service.record_failure(db, payload.email)
        else:
            await lockout_service.record_unknown_failure(db, payload.email)
        if user is not None:
            await _record_login_failure(db, user, request)
        await _commit_auth_accounting(db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Deprovisioning: an identity the platform disabled cannot authenticate, and
    # neither can a person every org of theirs has offboarded (each membership
    # deactivated).
    if not user.is_active:
        raise _account_deactivated()

    # A sign-in names no org: it lands in the most recently used org whose
    # sign-in policy admits a password (the home org for a person in one org).
    #
    # SSO enforcement: if that org enforces SSO for this identity, password
    # login is rejected server-side (the `enforced` flag is a real boundary,
    # not just a UI redirect). Decided by the org being entered, never by
    # whichever org happens to claim the email's domain — a domain claim is
    # unverified, so a foreign tenant must not be able to govern this account.
    # Platform staff and the org's break-glass members pass, so a broken IdP
    # can never lock the org out.
    landing = await org_landing(db, user, method=METHOD_PASSWORD)
    entering = landing.org_team_id
    membership = await membership_in(db, user_id=user.id, org_team_id=entering)
    if membership is not None and not membership.is_active:
        raise _account_deactivated()
    # With multi-org on, a person may have left (or been removed from) every
    # org they belonged to. They still sign in, into no org: see
    # `_land_without_org`.
    without_org = multi_org_enabled() and membership is None
    policy = landing.refusal
    if policy is not None:
        await _audit_step_up(db, user, entering, policy.kind)
        await _commit_auth_accounting(db)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": policy.kind,
                "message": (
                    "Your organization requires single sign-on."
                    if policy.kind == "sso_required"
                    else "Your organization doesn't allow this sign-in method."
                ),
            },
        )

    # Second factor (TOTP / backup code) for MFA-enabled accounts. A missing code
    # asks the SPA to collect one (mfa_required); a wrong code counts toward lockout.
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "mfa_required", "message": "Enter your authenticator code."},
            )
        if not await mfa_service.verify_code_locked(db, user, payload.mfa_code):
            await lockout_service.record_failure(db, payload.email)
            await _commit_auth_accounting(db)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "mfa_invalid", "message": "Invalid authentication code."},
            )

    # A fully successful authentication clears the counter — deliberately after
    # the second factor, so a correct password alone can't reset it (and so the
    # ORM has nothing pending that would overwrite the atomic failure UPDATE).
    lockout_service.reset(user)
    # And any counter the address accumulated before it had an account: a
    # sprayed address must not arrive pre-locked the day someone registers it.
    await lockout_service.clear_unknown(db, payload.email)
    # Abuse forensics: where this session came from (admin-only surface). Only
    # a PARSEABLE address overwrites — the first X-Forwarded-For hop is
    # client-spoofable, so a malformed header must not become an erase-my-trail
    # primitive; the column keeps the last KNOWN login IP.
    forensics_ip = _forensics_ip(request)
    if forensics_ip is not None:
        user.last_login_ip = forensics_ip

    if without_org:
        return await _land_without_org(db, user, request=request, method=METHOD_PASSWORD)
    claims = await issue_session(
        db, user, request=request, response=response, method=METHOD_PASSWORD, org_team_id=entering
    )
    emit_event(EventName.user_logged_in, user_id=user.id, org_id=claims.org_team_id)
    await org_audit_service.record(
        db, org_id=claims.org_team_id, actor=user, action="auth.login", target=user.email
    )
    return LoginResponse(
        user=await user_read(db, user, claims.org_team_id),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
        choose_org=landing.choose_org,
    )


# --------------------------------------------------------------------------- #
# TOTP multi-factor auth (per-user)
# --------------------------------------------------------------------------- #


@router.get("/mfa/status", response_model=MfaStatusResponse)
async def mfa_status(user: CurrentUser) -> MfaStatusResponse:
    return MfaStatusResponse(
        enabled=user.mfa_enabled,
        backup_codes_remaining=mfa_service.backup_codes_remaining(user),
    )


class MfaEnrollRequest(BaseModel):
    """Step-up proof for turning MFA on.

    Optional: a caller whose session was minted within
    ``_MFA_ENROLL_AUTH_FRESHNESS`` has already proved a factor and sends nothing.
    An older session on a password account has to re-present the password here.
    """

    current_password: str | None = Field(default=None, max_length=255)


def _authenticated_within(request: Request, *, window: timedelta) -> bool:
    """Whether the credential presenting this request was itself minted inside
    `window` — i.e. the caller authenticated that recently."""
    claims = optional_session_claims(request)
    if claims is None:  # pragma: no cover - CurrentUser already decoded it
        return False
    issued_at = datetime.fromtimestamp(claims.issued_at, tz=UTC)
    return datetime.now(UTC) - issued_at <= window


async def _require_enrollment_step_up(
    db: DbSession, user: User, request: Request, current_password: str | None
) -> None:
    """Demand proof of a current factor before a second factor is planted.

    Enabling MFA is as consequential as disabling it, and far harder to undo:
    `/mfa/disable` and the credential-change step-up both demand a code, but a
    factor enrolled by someone else can be removed by nobody — the owner cannot
    produce its code, a password reset does not clear it, and no admin reset
    exists. So a session lifted from a laptop backup or an unlocked machine must
    not be enough on its own to lock the real owner out of a verified account
    permanently.

    Either proof suffices:
    - the session was minted moments ago, so the caller just presented the
      account's real credentials (this is also the only proof available to an
      account whose sole credential is a federated provider), or
    - the account's current password.

    A wrong password is a guess against the real credential, so it goes through
    the same `lockout_service` budget `/auth/login` uses and is committed even
    though the request rolls back; an ABSENT one is a prompt, not a guess, and is
    not counted. Every refusal carries a machine-readable `code` so the SPA can
    tell "sign in again" from "type your password" from "that was wrong".

    Only the enroll leg is gated: `/mfa/confirm` cannot activate anything without
    the pending secret this route issues, so one control covers the whole flow.
    """
    _assert_not_locked(user)
    if account_wide_controls_guarded():
        # A token minted moments ago proves nothing here: every refresh mints
        # one, and a session an org's IdP started is that org's, not the
        # person's. The proof is a sign-in as the person, recently.
        # The account's current password, below, proves it as well.
        standing = await account_standing(db, request, user)
        if standing.within(_MFA_ENROLL_AUTH_FRESHNESS):
            return
        if not standing.proven and user.password_hash is None:
            raise account_sign_in_required(standing)
    elif _authenticated_within(request, window=_MFA_ENROLL_AUTH_FRESHNESS):
        return
    if user.password_hash is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "reauth_required",
                "message": "Sign in again to turn on two-factor authentication.",
            },
        )
    if not current_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "current_password_required",
                "message": "Your current password is required to turn on "
                "two-factor authentication.",
            },
        )
    if not verify_password(current_password, user.password_hash):
        await lockout_service.record_failure(db, user.email)
        await _commit_auth_accounting(db)
        raise await lockout_service.wrong_factor(
            db, user, code="current_password_invalid", wrong="That password isn't correct."
        )
    # Every factor proved — clear the streak, exactly as a successful login does,
    # so earlier typos can't accumulate into a lockout for someone who does know
    # the credential.
    lockout_service.reset(user)


@router.post("/mfa/enroll", response_model=MfaEnrollResponse, dependencies=[_auth_throttle])
async def mfa_enroll(
    user: CurrentUser,
    db: DbSession,
    request: Request,
    payload: MfaEnrollRequest | None = None,
) -> MfaEnrollResponse:
    """Generate a pending TOTP secret + the otpauth URI for an authenticator app.
    Activated by /mfa/confirm with a valid code."""
    await _require_enrollment_step_up(
        db, user, request, payload.current_password if payload is not None else None
    )
    try:
        secret, uri = mfa_service.begin_enrollment(user)
    except MfaError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await db.flush()
    return MfaEnrollResponse(secret=secret, otpauth_uri=uri)


def _assert_not_locked(user: User) -> None:
    """Refuse a credential-verifying MFA operation while the account is locked out.

    The 6-digit code space is small enough to grind online, so every route that
    checks one shares the login path's throttle — otherwise a hijacked session
    could brute-force MFA off at whatever rate the network allows. The enroll
    step-up checks the password rather than a code and shares it for the same
    reason."""
    expiry = lockout_service.lock_expiry(user)
    if expiry is not None:
        raise lockout_service.locked_out(expiry)


async def _record_mfa_code_failure(db: DbSession, user: User) -> None:
    """Count a rejected MFA code against the account's lockout budget + commit
    (the 400 that follows rolls the request back)."""
    await lockout_service.record_failure(db, user.email)
    await _commit_auth_accounting(db)


@router.post("/mfa/confirm", response_model=MfaConfirmResponse, dependencies=[_auth_throttle])
async def mfa_confirm(
    payload: MfaCodeRequest, user: CurrentUser, org_id: CurrentOrg, request: Request, db: DbSession
) -> MfaConfirmResponse:
    """Activate MFA after proving a valid code; returns single-use backup codes."""
    _assert_not_locked(user)
    # Only a rejected CODE is a guess against the 6-digit space. The other
    # refusals here are state errors ("MFA is already enabled", "Start enrollment
    # first"); charging those to the lockout budget would let a handful of benign
    # double-submits lock the owner out of login. Mirrors `confirm_enrollment`'s
    # own precondition order — past these two it can only fail on the code.
    # Read AFTER the lock: a concurrent confirmation that won the race has already
    # activated the factor and spent the code, so deciding this from the pre-lock
    # state would charge the loser's benign double-submit to the lockout budget.
    await mfa_service.lock_account(db, user)
    code_is_checked = not user.mfa_enabled and bool(user.mfa_secret_encrypted)
    try:
        codes = mfa_service.confirm_enrollment(user, payload.code)
    except MfaError as exc:
        if code_is_checked:
            await _record_mfa_code_failure(db, user)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.mfa_enabled",
        org_team_id=org_id,
        client=client_hint(request),
    )
    return MfaConfirmResponse(backup_codes=codes)


@router.post("/mfa/disable", response_model=MessageResponse, dependencies=[_auth_throttle])
async def mfa_disable(
    payload: MfaCodeRequest, user: CurrentUser, org_id: CurrentOrg, request: Request, db: DbSession
) -> MessageResponse:
    """Turn MFA off — requires a current code (a hijacked session can't disable it
    freely, and a wrong code counts toward the account lockout)."""
    _assert_not_locked(user)
    # As in /mfa/confirm: only a rejected code is a guess. "MFA is not enabled"
    # is a state error and must not spend the account's failure budget.
    # As in /mfa/confirm, read after the lock: a concurrent disable that won the
    # race has already turned the factor off.
    await mfa_service.lock_account(db, user)
    code_is_checked = user.mfa_enabled
    try:
        mfa_service.disable(user, payload.code)
    except MfaError as exc:
        if code_is_checked:
            await _record_mfa_code_failure(db, user)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.mfa_disabled",
        org_team_id=org_id,
        client=client_hint(request),
    )
    return MessageResponse(message="MFA disabled")


@router.post("/refresh", response_model=LoginResponse, dependencies=[_auth_throttle])
async def refresh(
    request: Request, response: Response, db: DbSession
) -> LoginResponse | JSONResponse:
    """Exchange the refresh cookie for a fresh access token and a rotated
    refresh token. Reuse of a rotated token ends the whole family; every
    refusal clears both cookies so the browser lands on login instead of
    retrying a dead credential. The refresh cookie's ``Path`` means it
    reaches only this route."""
    raw = request.cookies.get(settings.auth_refresh_cookie_name)
    if not raw:
        return refresh_refused(request, code="unauthorized", message="missing refresh token")
    hint = client_hint(request)
    try:
        minted, user = await rotate_refresh_token(
            db, raw, user_agent=hint.user_agent, ip_prefix=hint.ip_prefix
        )
    except RefreshError as exc:
        return await answer_refresh_error(db, request, exc)
    _, banned = await user_service.get_by_id_with_ban(db, user.id)
    if banned:
        await revoke_all_for_user(db, user.id)
        await db.commit()
        return refresh_refused(request, code="unauthorized", message="user no longer exists")
    # The org the family is in decides, at every refresh, whether the way the
    # family signed in still admits it: an SSO org's session lasts at most the
    # connection's max age without a round trip to its IdP.
    org = active_org_of(minted.row, user)
    policy = await sign_in_policy.evaluate(
        db, user=user, org_team_id=org, family_id=minted.row.family_id, method=None
    )
    if isinstance(policy, sign_in_policy.StepUp):
        await refuse_family_org(db, minted.row.family_id)
        await org_audit_service.record(
            db, org_id=org, actor=user, action=f"auth.{policy.kind}", target=user.email
        )
        await db.commit()
        return step_up_refused(request, policy=policy, refresh_raw=minted.raw)
    try:
        claims = await issue_rotated_session(db, user, minted, response=response)
    except MembershipRefused as exc:
        # The rotation admitted the family's org a moment ago and the
        # membership ended in between: answer as the rotation would have.
        await db.rollback()
        return refresh_refused(request, code=exc.code, message="membership refused")
    return LoginResponse(
        user=await user_read(db, user, claims.org_team_id),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
    )


#: What a sign-in answers for a person who belongs to no org (multi-org on).
NO_ACTIVE_MEMBERSHIP_CODE = "no_active_membership"


async def _land_without_org(
    db: DbSession, user: User, *, request: Request, method: str
) -> JSONResponse:
    """Sign in a person who has no org to enter.

    Every access token names a membership, so none is minted. The login
    session itself starts (a refresh token naming no org, carrying the
    sign-in's grant) and the answer is a 403 ``no_active_membership``: the
    client offers to create an org (``POST /auth/refresh/org/new``) or accept an
    invitation link (``POST /auth/refresh/org/join``), both of which act on this
    session and enter the org they produce. Nothing else is reachable."""
    hint = client_hint(request)
    minted = await mint_refresh_token(
        db,
        user_id=user.id,
        access_jti=None,
        user_agent=hint.user_agent,
        ip_prefix=hint.ip_prefix,
        active_org_team_id=None,
    )
    await record_grant(
        db,
        family_id=minted.row.family_id,
        org_team_id=None,
        method=method,
        at=minted.row.created_at,
    )
    await record_security_event(
        db, user_id=user.id, event="auth.signed_in", client=hint, detail={"method": method}
    )
    await db.commit()
    trace_id = getattr(request.state, "trace_id", None) or get_trace_id() or new_trace_id()
    resp = JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=build_error_body(
            code=NO_ACTIVE_MEMBERSHIP_CODE,
            message="You're not a member of any organization.",
            trace_id=trace_id,
            status=status.HTTP_403_FORBIDDEN,
        ),
    )
    clear_session_cookie(resp)
    set_refresh_cookie(resp, minted.raw)
    return resp


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response, db: DbSession) -> MessageResponse:
    """Revoke the current token server-side AND clear both cookies.

    Idempotent: a missing/expired token still clears the cookies and returns OK.
    Works for both the SPA (cookie) and the CLI/daemon (Bearer). An EXPIRED
    access token is still read: it names the refresh family, and signing out
    must end that family — the refresh cookie itself never reaches this route.
    """
    claims = optional_session_claims(request, allow_expired=True)
    if claims is not None and claims.jti is not None:
        await revoke_jti(db, claims.jti)
        await revoke_family_of_access_token(db, claims.jti, reason=REASON_LOGOUT)
        emit_event(EventName.user_logged_out, user_id=claims.user_id, org_id=claims.org_team_id)
        if await user_service.get_by_id(db, claims.user_id) is not None:
            await record_security_event(
                db,
                user_id=claims.user_id,
                event="auth.logout",
                org_team_id=claims.org_team_id,
                client=client_hint(request),
            )
        # Nobody is on the chat page any more: the chat warmed for them goes
        # with the session, so no agent sits open for a person who left.
        await spare_service.reap_for_user(db, user_id=claims.user_id)
    clear_session_cookies(response)
    return MessageResponse(message="Logged out")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    request: Request, user: CurrentUser, org_id: CurrentOrg, response: Response, db: DbSession
) -> MessageResponse:
    """Revoke every session for the caller (bumps token_epoch, the registry and
    every refresh family), then keep the browser that asked signed in.

    The revocation is committed before anything else happens, so nothing that
    follows can undo it. The page promises "You stay signed in on this
    device": a caller that came with the session cookie gets a fresh session
    past the new ``token_epoch``, in the org the request is in, when that
    org's sign-in policy still admits the session it replaces; otherwise this
    browser is signed out too. A Bearer caller has no cookie session to keep
    and ends signed out with everything else.

    While multi-org is on, only a session that signed in as the person (not
    through an org's IdP) may end their sessions in every org."""
    await require_account_standing(db, request, user)
    await revoke_all_for_user(db, user.id)
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.sessions_revoked_all",
        org_team_id=org_id,
        client=client_hint(request),
    )
    await db.commit()
    if request.cookies.get(COOKIE_NAME):
        await reissue_session(
            db, user, request=request, response=response, method=METHOD_RENEWAL, org_team_id=org_id
        )
    else:
        clear_session_cookies(response)
    return MessageResponse(message="Signed out everywhere else")


async def _acting_family(db: DbSession, request: Request) -> UUID | None:
    """The login session the request's own access token belongs to."""
    jti = acting_session_jti(request)
    family = await family_of_access_token(db, jti) if jti is not None else None
    return family.family_id if family is not None else None


def _family_id(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request, user: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> SessionListResponse:
    """List the caller's live sessions (metadata only — never token values):
    every browser session as its refresh family (a family is the identity's,
    whichever org it is in), then every CLI token minted in the request's org.
    A token for the person's membership of another org is that org's."""
    current = optional_session_claims(request)
    current_jti = current.jti if current is not None else None
    families = await list_live_families(db, user.id)
    named = await family_of_access_token(db, current_jti) if current_jti is not None else None
    current_family = named.family_id if named is not None else None
    if not await speaks_for_account(db, request, user):
        # A session that does not stand for the account sees only what is in
        # its own org: its own browser sessions there, never the person's
        # devices and networks elsewhere.
        families = [
            f for f in families if f.family_id == current_family or f.active_org_team_id == org_id
        ]
    sessions = [
        SessionRead(
            jti=f.family_id.hex,
            token_type=TokenType.SESSION,
            issued_at=f.family_started_at,
            expires_at=f.absolute_expires_at,
            last_used_at=f.last_used_at or f.created_at,
            label=None,
            client=" · ".join(part for part in (f.user_agent, f.ip_prefix) if part) or None,
            current=(f.family_id == current_family),
        )
        for f in families
    ]
    sessions.extend(
        SessionRead(
            jti=t.jti,
            token_type=t.token_type,
            issued_at=t.issued_at,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            label=t.label,
            current=(t.jti == current_jti),
        )
        for t in await list_active_tokens(db, user.id, org_team_id=org_id)
        if t.token_type is not TokenType.SESSION
    )
    return SessionListResponse(sessions=sessions)


@router.delete("/sessions/{jti}", response_model=MessageResponse)
async def revoke_session(
    jti: PathId, request: Request, user: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> MessageResponse:
    """End one of the caller's sessions: a browser session by its family id
    (its live access token goes with it), else a token by jti in the
    request's org. 404 if it isn't theirs, or is their token for another org.

    A browser session belongs to the person, whichever org it is in, so while
    multi-org is on only a session that signed in as the person may end one
    other than its own. A token is ended within the request's org, as
    always."""
    family_id = _family_id(jti)
    if (
        family_id is not None
        and family_id != await _acting_family(db, request)
        and await is_family_of(db, family_id, user.id)
    ):
        await require_account_standing(db, request, user)
    if family_id is not None and await revoke_family_for_user(
        db, family_id, user.id, reason=REASON_USER
    ):
        await _record_session_revoked(db, user, org_id, request, kind="browser")
        return MessageResponse(message="Session revoked")
    if not await revoke_jti_for_user(db, jti, user.id, org_team_id=org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    await revoke_family_of_access_token(db, jti, reason=REASON_USER)
    await _record_session_revoked(db, user, org_id, request, kind="token")
    return MessageResponse(message="Session revoked")


async def _record_session_revoked(
    db: DbSession, user: User, org_id: UUID, request: Request, *, kind: str
) -> None:
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.session_revoked",
        org_team_id=org_id,
        client=client_hint(request),
        detail={"kind": kind},
    )


@router.get(
    "/gate/return",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    summary="Send the browser back to the app after a top-level visit to the API host",
)
async def gate_return(
    next_path: str | None = Query(default=None, alias="next", max_length=2048),
) -> RedirectResponse:
    """Return a browser to the app once it has visited this host top-level.

    An environment may put a sign-in gate in front of the API host itself (an
    OIDC action at the load balancer). Such a gate sets its session cookie only
    on a top-level navigation, and the app's own requests, cross-origin, would
    only ever meet the gate's redirect. The app sends the browser here once; the
    gate runs on the way in, and this answer returns it to where it was: a path
    under the app's own origin, never anywhere else (``safe_return_path``).
    """
    response = RedirectResponse(frontend_url(safe_return_path(next_path)), status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/me", response_model=MeRead)
async def me(request: Request, user: CurrentUser, org_id: CurrentOrg, db: DbSession) -> MeRead:
    read = await user_read(db, user, org_id)
    claims = optional_session_claims(request)
    expires = datetime.fromtimestamp(claims.expires_at, tz=UTC) if claims is not None else None
    return MeRead(**read.model_dump(), session_expires_at=expires)


@router.get("/identities", response_model=LinkedIdentityList)
async def list_identities(user: CurrentUser, db: DbSession) -> LinkedIdentityList:
    """External-login providers linked to the caller (no secrets)."""
    rows = (
        (
            await db.execute(
                select(OAuthIdentity)
                .where(OAuthIdentity.user_id == user.id)
                .order_by(OAuthIdentity.created_at)
            )
        )
        .scalars()
        .all()
    )
    return LinkedIdentityList(identities=[LinkedIdentity.model_validate(r) for r in rows])


@router.post("/complete-profile", response_model=UserRead)
async def complete_profile(
    payload: CompleteProfileRequest, user: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> UserRead:
    """Finish a profile provisioned without one (minimal signup, JIT/SSO).

    Sets the caller's name. `org_name`, when present, renames the caller's org —
    but only when they ADMIN it (a new-org signup makes them the org admin); it is
    ignored for an invited member, who doesn't own the org they joined.
    """
    updated = await user_service.update_profile(
        db, user, first_name=payload.first_name, last_name=payload.last_name
    )
    if payload.org_name is not None and await user_is_org_admin(db, user, org_team_id=org_id):
        org = await db.get(Team, org_id)
        if org is not None:
            await team_service.rename(db, org, payload.org_name)
    return await user_read(db, updated, org_id)


@router.post("/password/send-reset", response_model=MessageResponse, dependencies=[_auth_throttle])
async def send_password_setup(user: CurrentUser, db: DbSession) -> MessageResponse:
    """Email the caller a password set/change link (reuses the reset flow).

    Works whether or not a local password exists — OAuth-only users use this to
    set their first password, password users to change theirs. The caller is
    already authenticated, so there's nothing to leak.
    """
    if _issued_within_cooldown(
        user.password_reset_expires_at, ttl=password_reset_service.DEFAULT_TTL
    ):
        raise _resend_too_soon()
    token = await password_reset_service.issue_token(db, user)
    await record_security_event(
        db, user_id=user.id, event="auth.password_reset_requested", detail={"source": "account"}
    )
    await send_password_reset(user, token=token)
    return MessageResponse(message="A password link is on its way to your email")


@router.post(
    "/signup",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_auth_throttle],
)
async def signup(
    payload: SignupRequest, request: Request, response: Response, db: DbSession
) -> LoginResponse:
    await verify_turnstile(payload.turnstile_token, remote_ip=_client_ip(request))
    if payload.invite_token is None and not settings.public_signup_open:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "signup_closed",
                "message": "Sign-up is by invitation only. Ask an admin to invite you.",
            },
        )
    if payload.org_name is not None and payload.invite_token is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either an organization name or an invitation token, not both",
        )
    # A banned address — the account's own ban, or its domain's — is refused
    # before the existence check, and exactly as an address that fails email
    # validation: the ban reads as "enter a valid email", never as "this
    # account exists".
    if await ban_service.email_is_banned(db, payload.email):
        raise ban_service.invalid_email_refusal(payload.email, loc=("body", "email"))
    if await user_service.get_by_email(db, payload.email) is not None:
        if multi_org_enabled():
            # The person may be joining a second org with the address they
            # already use: send them to sign in, keeping the invitation.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=account_exists_detail(payload.invite_token),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists. Sign in instead.",
        )
    # Disposable-mail gate. Unlike the business-email gate below, there is NO
    # escape hatch: throwaway inboxes exist to farm accounts, and a legitimate
    # user always has a real address to use instead. Public signups only — an
    # invited address was vouched for by an org admin.
    if payload.invite_token is None and is_disposable_email(payload.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "disposable_email_blocked",
                "domain": email_domain(payload.email),
                "message": "Disposable email addresses can't be used to sign up. "
                "Please use your work email.",
            },
        )
    # Business-email gate. A structured `detail` lets the SPA recognize this
    # specific case (show the "use your work email" modal) vs a generic error.
    # The user can proceed by re-submitting with `allow_personal_email=true`.
    if not payload.allow_personal_email and is_personal_email(payload.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "personal_email_blocked",
                "domain": email_domain(payload.email),
                "message": "Please sign up with your work email.",
            },
        )

    # After the address gates (so each keeps answering with its own specific code)
    # and before any account exists. A password that IS the account's own email,
    # or is built from it, is the first thing a credential-stuffing run tries --
    # the address is the one secret an attacker targeting this account already has.
    enforce_password(
        payload.password,
        email=payload.email,
        names=(payload.first_name, payload.last_name),
    )

    if payload.invite_token is not None:
        invitation = await invitation_service.get_by_token(db, payload.invite_token)
        if invitation is None or invitation.status.value != "pending":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found"
            )
        if invitation.email != payload.email.lower().strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email does not match the invited address",
            )
        chain = await team_service.ancestor_chain(db, invitation.team_id)
        if not chain:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invitation target missing"
            )
        org_root = chain[-1]
        try:
            user = await user_service.create_user(
                db,
                org_team_id=org_root.id,
                email=payload.email,
                first_name=payload.first_name,
                last_name=payload.last_name,
                password=payload.password,
            )
        except UserConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        try:
            await invitation_service.accept_invitation(
                db, invitation, user=user, actor=actor_for_user(user, org_id=org_root.id)
            )
        except InvitationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # Invite-driven signup: the inviter already vouched for this address
        # (the invite was sent there and the recipient clicked through), so
        # we mark the email verified immediately. No banner shown.
        await email_verification_service.mark_verified(db, user, by_user=True)
        # Verified on creation → this user never hits the verify-email route, so
        # welcome them here. Once-ever guarded.
        await welcome_service.send_welcome_if_unsent(db, user)
    else:
        # Bare signup: create a new org with this user as initial admin. `org_name`
        # is optional now — a minimal (email + password) signup creates an UNNAMED
        # org ("") that the caller names on the complete-profile step.
        try:
            _org, user = await team_service.create_org_with_admin(
                db,
                org_name=payload.org_name or "",
                admin_email=payload.email,
                admin_first_name=payload.first_name,
                admin_last_name=payload.last_name,
                admin_password=payload.password,
            )
        except UserConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # Bare signup: nobody has vouched for the email — issue a token and
        # send the verification link. Failure to send doesn't block signup.
        verification_token = await email_verification_service.issue_token(db, user)
        # A public route: the relay round-trip stays off its response time.
        send_in_background(
            send_email_verification(user, token=verification_token),
            name=f"signup-verification-email:{user.id}",
        )

    # Abuse forensics: record where the account was created from (and seed the
    # last-login mark — this response IS a login). X-Forwarded-For-aware, same
    # resolution the Turnstile check uses.
    user.signup_ip = _forensics_ip(request)
    user.last_login_ip = user.signup_ip

    # What a distribution records about a new signup; never affects what the
    # user was provisioned.
    await run_signup_hooks(request.cookies, db, user)

    claims = await issue_session(db, user, request=request, response=response, method=METHOD_SIGNUP)
    via = "invite" if payload.invite_token is not None else "org"
    emit_event(EventName.user_signed_up, user_id=user.id, org_id=claims.org_team_id, via=via)
    if payload.invite_token is None:
        emit_event(EventName.org_created, user_id=user.id, org_id=claims.org_team_id)
    return LoginResponse(
        user=await user_read(db, user, claims.org_team_id),
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
    )


@router.post("/verify-email/resend", response_model=MessageResponse, dependencies=[_auth_throttle])
async def resend_email_verification(user: CurrentUser, db: DbSession) -> MessageResponse:
    if user.email_verified_at is not None:
        return MessageResponse(message="Email already verified")
    available_at = email_verification_service.resend_available_at(user)
    if available_at is not None and (remaining := available_at - datetime.now(UTC)) > timedelta():
        raise _resend_too_soon(retry_after=remaining)
    if not await email_verification_service.issue_and_send(db, user, send=send_email_verification):
        raise EmailSendFailedError(
            "We couldn't send the verification email. Try again in a few minutes."
        )
    return MessageResponse(message="Verification email sent")


@router.post("/verify-email/{token}", response_model=UserRead, dependencies=[_auth_throttle])
async def verify_email(token: PathId, db: DbSession) -> UserRead:
    """Public — anyone with the token can complete verification.

    Idempotency: if the token is for an already-verified user, we return
    409 (rather than silently succeed) so the SPA can show the right message.
    """
    try:
        user = await email_verification_service.consume_token(db, token)
    except VerificationError as exc:
        msg = str(exc)
        if "already verified" in msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc
    # First successful verification → send the one-time welcome. `consume_token`
    # 409s on a replay before reaching here, and the column guard is belt-and-braces.
    await welcome_service.send_welcome_if_unsent(db, user)
    # A public route with no session: the read describes the identity's home
    # org, the one its next sign-in enters.
    home = home_org_id(user)
    emit_event(EventName.email_verified, user_id=user.id, org_id=home)
    return await user_read(db, user, home)


class PasswordResetRequest(BaseModel):
    email: EmailStr
    # Cloudflare Turnstile token from the SPA's invisible captcha (verified before
    # issuing/sending a reset when Turnstile is configured; ignored otherwise).
    turnstile_token: str | None = None


class PasswordResetConfirm(BaseModel):
    password: str = Field(min_length=8, max_length=255, json_schema_extra=USER_PASSWORD_SCHEMA)


@router.post(
    "/password-reset/request", response_model=MessageResponse, dependencies=[_auth_throttle]
)
async def request_password_reset(
    payload: PasswordResetRequest, request: Request, db: DbSession
) -> MessageResponse:
    """Issue a reset token if the email matches a known account.

    Always returns 200 with the same message regardless of whether the
    email exists, to avoid leaking which addresses have accounts. SMTP
    failure is logged inside `send_password_reset`, not surfaced.
    """
    await verify_turnstile(payload.turnstile_token, remote_ip=_client_ip(request))
    user, banned = await user_service.get_by_email_with_ban(db, payload.email)
    # Inside the cooldown the outstanding link stays valid and nothing is sent —
    # silently, and with the same constant response, so this route still can't be
    # used to tell which addresses have accounts. A banned account is an unknown
    # address here: nothing is sent, the same message comes back.
    if (
        user is not None
        and not banned
        and not _issued_within_cooldown(
            user.password_reset_expires_at, ttl=password_reset_service.DEFAULT_TTL
        )
    ):
        token = await password_reset_service.issue_token(db, user)
        await record_security_event(
            db,
            user_id=user.id,
            event="auth.password_reset_requested",
            client=client_hint(request),
            detail={"source": "sign_in_page"},
        )
        # Off the request path: awaiting the relay only when the address has an
        # account would make the response time answer the question the constant
        # body refuses to.
        send_in_background(
            send_password_reset(user, token=token), name=f"password-reset-email:{user.id}"
        )
    return MessageResponse(message="If that email matches an account, a reset link is on its way")


@router.post(
    "/password-reset/{token}", response_model=MessageResponse, dependencies=[_auth_throttle]
)
async def confirm_password_reset(
    token: PathId, payload: PasswordResetConfirm, request: Request, db: DbSession
) -> MessageResponse:
    """Public — anyone with a valid token can set a new password."""
    try:
        user = await password_reset_service.consume_token(
            db,
            token,
            new_password=payload.password,
            # Runs only after the token resolves to a real account, so the policy
            # can be checked against that identity without answering "does this
            # address exist?" for a caller holding no valid token.
            validate=lambda account: enforce_password(
                payload.password,
                email=account.email,
                names=(account.first_name, account.last_name),
            ),
        )
    except PasswordResetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await record_security_event(
        db, user_id=user.id, event="auth.password_reset_completed", client=client_hint(request)
    )
    return MessageResponse(message="Password updated")


__all__ = ["COOKIE_NAME", "router"]
