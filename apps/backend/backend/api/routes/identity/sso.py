"""Enterprise SSO routes (per-org OIDC).

    GET /api/v1/auth/sso/discover?email=   → does this email's domain use SSO? (login URL)
    GET /api/v1/auth/sso/{org_id}/login    → set state cookie, redirect to the org's IdP
    GET /api/v1/auth/sso/{org_id}/callback → exchange code, resolve (cross-org gate), session

The session-minting tail mirrors `oauth.py`; the difference is that the provider
is built per-org from the stored connection and `oauth_service.resolve` is called
with an `IdpScope`, so the IdP can only ever act within its org and the email
domains platform staff assigned to that org.

A successful assertion is a grant FOR THE CONNECTION'S ORG on the browser's
login session (``auth_session_org_grants``). A browser already signed in as the
same person keeps its session and gains the grant (a step-up, e.g. after a
refresh answered ``sso_required``); otherwise a new session starts.

With multi-org on, an assertion for an identity that exists but holds no
active membership in the org is parked instead (``services.identity.sso_link``):
the browser gets a cookie naming it and lands on ``/link-sso``, where the
person signs in with one of the identity's own methods and confirms or cancels
the link through ``link_router``:

    GET  /api/v1/auth/sso-link          → the parked request, for the signed-in identity
    POST /api/v1/auth/sso-link/confirm  → link (and join, when the org asked for them)
    POST /api/v1/auth/sso-link/cancel   → discard it
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID
from xml.sax.saxutils import quoteattr

from alkera_core.auth import (
    InvalidTokenError,
    decode_oauth_state,
    encode_oauth_state,
    sign_in_policy,
    sso_domains,
)
from alkera_core.auth.refresh import family_of_access_token
from alkera_core.config import settings
from alkera_core.models import SsoConnection, User
from alkera_core.schemas.identity.sso import (
    ScimTokenResponse,
    SsoConnectionRead,
    SsoConnectionUpdateRequest,
    SsoDiscoverResponse,
    SsoExemptMember,
    SsoExemptUpdateRequest,
    SsoLinkCancelResponse,
    SsoLinkConfirmResponse,
    SsoLinkRead,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.rate_limit import limited
from backend.api.return_path import safe_return_path
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    OrgAdmin,
    OrgAdminVerified,
    require_enterprise_features,
)
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.oidc import issuer_is_dialable
from backend.auth.oauth.saml import SamlProvider
from backend.auth.session_issue import (
    METHOD_SSO,
    acting_session_jti,
    client_hint,
    issue_session,
    issue_stepped_up_session,
    record_grant,
    stepped_up_family,
)
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.identity import (
    SSO_LINK_TTL,
    LoginOutcome,
    OAuthLoginBlockedError,
    SsoLinkError,
    SsoLinkOutcome,
    sso_link_cancel,
    sso_link_confirm,
    sso_link_view,
)
from backend.services.identity import oauth as oauth_service
from backend.services.identity import sso as sso_service
from backend.services.identity import users as user_service
from backend.services.org import members_of, membership_in
from backend.utils.cookies import (
    OAUTH_STATE_COOKIE,
    clear_oauth_state_cookie,
    set_oauth_state_cookie,
)

router = APIRouter(prefix="/api/v1/auth/sso", tags=["sso"])

#: The confirmation of a parked SSO link (see the module docstring).
link_router = APIRouter(prefix="/api/v1/auth/sso-link", tags=["sso"])

#: The cookie naming a parked SSO link request. HttpOnly, SameSite=Lax (it is
#: set on the IdP's redirect back) and sent only to the confirmation routes.
SSO_LINK_COOKIE = "alkera_sso_link"
SSO_LINK_COOKIE_PATH = "/api/v1/auth/sso-link"


def _set_link_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        key=SSO_LINK_COOKIE,
        value=raw,
        max_age=int(SSO_LINK_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path=SSO_LINK_COOKIE_PATH,
    )


def _clear_link_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SSO_LINK_COOKIE,
        path=SSO_LINK_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )


def _link_redirect(outcome: SsoLinkOutcome) -> RedirectResponse:
    """Send the browser to the confirmation page with the cookie naming the
    parked request. No session is issued and nothing is linked."""
    resp = RedirectResponse(_frontend(f"/link-sso?org={outcome.org_team_id}"), status_code=302)
    _set_link_cookie(resp, outcome.token)
    clear_oauth_state_cookie(resp)
    return resp


def _frontend(path: str) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}{path}"


def _login_error(reason: str) -> RedirectResponse:
    resp = RedirectResponse(_frontend(f"/login?oauth_error={reason}"), status_code=302)
    clear_oauth_state_cookie(resp)
    return resp


async def _sync_sso_role(
    db: AsyncSession,
    *,
    org_id: UUID,
    user: User,
    groups: tuple[str, ...],
    conn: SsoConnection,
) -> None:
    """Apply the org's IdP-group→role mapping to a just-authenticated SSO user and
    audit any change. Shared by the OIDC callback + the SAML ACS."""
    applied = await sso_service.sync_org_role(db, user=user, connection=conn, idp_groups=groups)
    if applied is not None:
        # Record the role applied + how many IdP groups were seen — NOT the raw
        # group names (they can be sensitive, e.g. "layoffs-2026", and aren't
        # redacted by the audit scrubber, which keys on secret-ish field names).
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=user,
            action="sso.role_synced",
            target=user.email,
            detail={"role": applied.value, "group_count": len(groups)},
        )


def _return_to(raw: str | None) -> str:
    """Where the browser lands after the IdP: a path under the app's own
    origin (``safe_return_path``), never anywhere else; the dashboard when
    none is given."""
    return safe_return_path(raw) if raw else "/dashboard"


def _callback_uri(org_id: UUID) -> str:
    return f"{settings.oauth_redirect_base}/api/v1/auth/sso/{org_id}/login/callback"


def _login_url(org_id: UUID, *, protocol: str) -> str:
    base = f"{settings.oauth_redirect_base}/api/v1/auth/sso/{org_id}"
    return f"{base}/saml/login" if protocol == "saml" else f"{base}/login"


@router.get(
    "/discover", response_model=SsoDiscoverResponse, dependencies=[Depends(limited("credential"))]
)
async def discover(db: DbSession, email: str = Query(...)) -> SsoDiscoverResponse:
    """Whether the email's domain is assigned to an org with an enabled SSO
    connection, so the login screen can route the user to that org's IdP.
    Reveals only SSO availability for a domain, never whether an account
    exists."""
    conn = await sso_service.find_enabled_connection_for_email(db, email)
    if conn is None:
        return SsoDiscoverResponse(sso=False, login_url=None)
    return SsoDiscoverResponse(
        sso=True,
        login_url=_login_url(conn.org_team_id, protocol=conn.protocol),
        enforced=conn.enforced,
    )


@router.get("/{org_id}/login", dependencies=[Depends(limited("credential"))])
async def login(
    org_id: UUID, db: DbSession, return_to: str | None = Query(default=None)
) -> Response:
    conn = await sso_service.get_connection(db, org_id)
    if conn is None or not conn.enabled:
        return _login_error("sso_not_configured")
    try:
        provider = sso_service.build_oidc_provider(conn)
    except ValueError:
        return _login_error("sso_misconfigured")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    tx = encode_oauth_state(
        state=state,
        nonce=nonce,
        provider=provider.key,
        intent="login",
        invite_token=None,
        return_to=_return_to(return_to),
    )
    try:
        url = await provider.authorization_url(
            redirect_uri=_callback_uri(org_id), state=state, nonce=nonce
        )
    except OAuthError:
        return _login_error("provider_unavailable")
    resp = RedirectResponse(url, status_code=302)
    set_oauth_state_cookie(resp, tx)
    return resp


@router.get("/{org_id}/login/callback", dependencies=[Depends(limited("credential"))])
async def callback(
    org_id: UUID,
    request: Request,
    db: DbSession,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
) -> Response:
    raw_tx = request.cookies.get(OAUTH_STATE_COOKIE)
    if not raw_tx:
        return _login_error("expired")
    try:
        tx = decode_oauth_state(raw_tx)
    except InvalidTokenError:
        return _login_error("expired")

    conn = await sso_service.get_connection(db, org_id)
    if conn is None or not conn.enabled:
        return _login_error("sso_not_configured")
    # CSRF: the returned state must match the signed one, for THIS org's provider.
    if (
        not code
        or not state
        or state != tx.state
        or tx.provider != sso_service.provider_key(org_id)
    ):
        return _login_error("state")
    try:
        provider = sso_service.build_oidc_provider(conn)
    except ValueError:
        return _login_error("sso_misconfigured")

    try:
        profile = await provider.fetch_profile(
            code=code, redirect_uri=_callback_uri(org_id), nonce=tx.nonce
        )
    except OAuthError:
        return _login_error("exchange")

    try:
        outcome = await oauth_service.resolve(
            db, profile, invite_token=None, idp_scope=await sso_service.scope_for(db, conn)
        )
    except OAuthLoginBlockedError as exc:
        return _login_error(exc.reason)
    if isinstance(outcome, SsoLinkOutcome):
        return _link_redirect(outcome)

    # SSO always logs in (it JIT-provisions when there's no account) — never a
    # registration ticket.
    assert isinstance(outcome, LoginOutcome)
    await _sync_sso_role(db, org_id=org_id, user=outcome.user, groups=profile.idp_groups, conn=conn)
    return await _complete_login(
        db, request, user=outcome.user, org_id=org_id, return_to=tx.return_to
    )


async def _complete_login(
    db: DbSession, request: Request, *, user: User, org_id: UUID, return_to: str | None
) -> Response:
    """Record the org's grant and sign the browser into the org, then redirect
    into the app. Shared by the OIDC callback and the SAML ACS.

    A browser already in a live session of the same person keeps it: the grant
    lands on that session and a fresh access token for the org is minted beside
    its waiting refresh token. Anyone else gets a new session."""
    resp = RedirectResponse(_frontend(_return_to(return_to)), status_code=302)
    policy = await sign_in_policy.evaluate(
        db, user=user, org_team_id=org_id, family_id=None, method=METHOD_SSO
    )
    if isinstance(policy, sign_in_policy.StepUp):
        return _login_error(policy.kind)
    family = await stepped_up_family(db, request, user)
    if family is None:
        await issue_session(
            db, user, request=request, response=resp, method=METHOD_SSO, org_team_id=org_id
        )
    else:
        await record_grant(
            db,
            family_id=family.family_id,
            org_team_id=org_id,
            method=METHOD_SSO,
            at=datetime.now(UTC),
        )
        await issue_stepped_up_session(db, user, family, org_team_id=org_id, response=resp)
        await record_security_event(
            db,
            user_id=user.id,
            event="auth.signed_in",
            org_team_id=org_id,
            client=client_hint(request),
            detail={"method": METHOD_SSO},
        )
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=user,
        action="auth.login",
        target=user.email,
        detail={"method": METHOD_SSO},
    )
    clear_oauth_state_cookie(resp)
    return resp


# --------------------------------------------------------------------------- #
# SAML (SP-initiated): AuthnRequest → IdP → signed Response POSTed to the ACS
# --------------------------------------------------------------------------- #


def _saml_provider_or_none(conn: SsoConnection | None) -> SamlProvider | None:
    if conn is None or not conn.enabled or conn.protocol != "saml":
        return None
    try:
        return sso_service.build_saml_provider(conn)
    except ValueError:
        return None


@router.get("/{org_id}/saml/login", dependencies=[Depends(limited("credential"))])
async def saml_login(
    org_id: UUID, db: DbSession, return_to: str | None = Query(default=None)
) -> Response:
    conn = await sso_service.get_connection(db, org_id)
    provider = _saml_provider_or_none(conn)
    if provider is None:
        return _login_error("sso_not_configured")
    # The AuthnRequest ID must be a valid XML id (start with '_'); the response's
    # InResponseTo must echo it. Stash it in the SIGNED state cookie — never trust
    # the attacker-visible RelayState for this binding.
    request_id = "_" + secrets.token_hex(16)
    tx = encode_oauth_state(
        state=request_id,
        nonce=secrets.token_urlsafe(16),
        provider=provider.key,
        intent="login",
        invite_token=None,
        return_to=_return_to(return_to),
    )
    resp = RedirectResponse(provider.authn_request_redirect_url(request_id=request_id), 302)
    set_oauth_state_cookie(resp, tx)
    return resp


@router.post("/{org_id}/saml/acs", dependencies=[Depends(limited("credential"))])
async def saml_acs(org_id: UUID, request: Request, db: DbSession) -> Response:
    raw_tx = request.cookies.get(OAUTH_STATE_COOKIE)
    if not raw_tx:
        return _login_error("expired")
    try:
        tx = decode_oauth_state(raw_tx)
    except InvalidTokenError:
        return _login_error("expired")
    if tx.provider != sso_service.provider_key(org_id):
        return _login_error("state")

    conn = await sso_service.get_connection(db, org_id)
    provider = _saml_provider_or_none(conn)
    if provider is None or conn is None:
        return _login_error("sso_not_configured")

    form = await request.form()
    saml_response = form.get("SAMLResponse")
    if not isinstance(saml_response, str) or not saml_response:
        return _login_error("exchange")
    try:
        # InResponseTo MUST equal the AuthnRequest id from the signed cookie.
        profile = provider.parse_response(saml_response, expected_in_response_to=tx.state)
    except OAuthError:  # SamlError subclasses OAuthError → one generic outcome
        return _login_error("exchange")

    # Replay defense: the assertion is single-use within its validity window.
    raw = profile.raw or {}
    assertion_id = str(raw.get("assertion_id") or "")
    expires_iso = str(raw.get("not_on_or_after") or "")
    if assertion_id and expires_iso:
        consumed = await sso_service.consume_saml_assertion(
            db,
            org_team_id=org_id,
            assertion_id=assertion_id,
            expires_at=datetime.fromisoformat(expires_iso),
        )
        if not consumed:
            return _login_error("replay")

    try:
        outcome = await oauth_service.resolve(
            db, profile, invite_token=None, idp_scope=await sso_service.scope_for(db, conn)
        )
    except OAuthLoginBlockedError as exc:
        return _login_error(exc.reason)
    if isinstance(outcome, SsoLinkOutcome):
        return _link_redirect(outcome)
    assert isinstance(outcome, LoginOutcome)
    await _sync_sso_role(db, org_id=org_id, user=outcome.user, groups=profile.idp_groups, conn=conn)
    return await _complete_login(
        db, request, user=outcome.user, org_id=org_id, return_to=tx.return_to
    )


# --------------------------------------------------------------------------- #
# Confirming a parked link (the browser's cookie + a session of the identity)
# --------------------------------------------------------------------------- #


def _link_refused(exc: SsoLinkError) -> HTTPException:
    if exc.code is None:
        return HTTPException(status_code=exc.status_code, detail=exc.message)
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
    )


@link_router.get("", response_model=SsoLinkRead)
async def get_sso_link(request: Request, user: CurrentUser, db: DbSession) -> SsoLinkRead:
    """The parked request this browser's cookie names, for the signed-in
    identity: 404 when there is none (missing, expired, consumed, or multi-org
    off), 409 ``sso_link_other_account`` when the browser is signed in as an
    identity the request is not about."""
    try:
        found = await sso_link_view(db, request.cookies.get(SSO_LINK_COOKIE), user=user)
    except SsoLinkError as exc:
        raise _link_refused(exc) from exc
    return SsoLinkRead(
        org_team_id=found.org_team_id, org_name=found.org_name, email_masked=found.email_masked
    )


@link_router.post(
    "/confirm", response_model=SsoLinkConfirmResponse, dependencies=[Depends(limited("credential"))]
)
async def confirm_sso_link(
    request: Request, response: Response, user: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> SsoLinkConfirmResponse:
    """Link the org's IdP subject to the signed-in identity and join the org
    when it asked for them (a pending invitation, or a membership its SCIM
    provisioned). Single use. Refusals: 404 (no live request), 409
    ``sso_link_other_account``, 409 ``sso_subject_linked`` (the subject is
    another identity's), 409 ``sso_provider_linked`` (this identity holds a
    different subject at the same IdP), 403 ``account_deactivated``."""
    try:
        result = await sso_link_confirm(db, request.cookies.get(SSO_LINK_COOKIE), user=user)
    except SsoLinkError as exc:
        raise _link_refused(exc) from exc
    await org_audit_service.record(
        db,
        org_id=result.org_team_id,
        actor=user,
        action="auth.sso_linked",
        target=user.email,
        detail={"joined": result.joined},
    )
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.sso_linked",
        org_team_id=org_id,
        client=client_hint(request),
        detail={"linked_org_team_id": str(result.org_team_id), "joined": result.joined},
    )
    _clear_link_cookie(response)
    return SsoLinkConfirmResponse(
        linked=True,
        joined=result.joined,
        org_team_id=result.org_team_id,
        message=None if result.joined else f"Ask an admin of {result.org_name} to invite you.",
    )


@link_router.post("/cancel", response_model=SsoLinkCancelResponse)
async def cancel_sso_link(
    request: Request, response: Response, _user: CurrentUser, db: DbSession
) -> SsoLinkCancelResponse:
    """Discard the parked request without linking anything. 404 when there is
    no live request."""
    try:
        await sso_link_cancel(db, request.cookies.get(SSO_LINK_COOKIE))
    except SsoLinkError as exc:
        raise _link_refused(exc) from exc
    _clear_link_cookie(response)
    return SsoLinkCancelResponse(cancelled=True)


@router.get("/{org_id}/saml/metadata")
async def saml_metadata(org_id: UUID) -> Response:
    """SP metadata the org admin hands to their IdP (entity id + ACS)."""
    sp = sso_service.saml_sp_config(org_id)
    # quoteattr returns a value WITH surrounding quotes, fully escaped — defense in
    # depth even though these are settings-derived (UUID + base URL).
    entity = quoteattr(sp.entity_id)
    acs = quoteattr(sp.acs_url)
    xml = (
        '<?xml version="1.0"?>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f"entityID={entity}>"
        "<md:SPSSODescriptor protocolSupportEnumeration="
        '"urn:oasis:names:tc:SAML:2.0:protocol" AuthnRequestsSigned="false" '
        'WantAssertionsSigned="true">'
        "<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>"
        "<md:AssertionConsumerService "
        'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location={acs} index="0" isDefault="true"/>'
        "</md:SPSSODescriptor></md:EntityDescriptor>"
    )
    return Response(content=xml, media_type="application/samlmetadata+xml")


# --------------------------------------------------------------------------- #
# Org-admin SSO configuration (separate router — auth-gated, org-scoped)
# --------------------------------------------------------------------------- #

# SSO is an Enterprise feature: on Alkera's SaaS a non-Enterprise org gets a 403
# here (the SPA shows a Contact-Sales gate); self-hosted installs pass through.
# Only the admin CRUD is gated — the public login/callback flow (`router`) stays
# open. Composes with each route's own org-admin dependency.
org_router = APIRouter(
    prefix="/api/v1/org/sso",
    tags=["sso"],
    dependencies=[Depends(require_enterprise_features)],
)


async def _read(db: AsyncSession, conn: SsoConnection | None) -> SsoConnectionRead:
    if conn is None:
        return SsoConnectionRead(
            configured=False,
            enabled=False,
            enforced=False,
            protocol="oidc",
            allowed_domains="",
            oidc_issuer=None,
            oidc_client_id=None,
            has_client_secret=False,
            saml_idp_entity_id=None,
            saml_sso_url=None,
            has_saml_cert=False,
            scim_base_url=sso_service.scim_base_url(),
        )
    sp = sso_service.saml_sp_config(conn.org_team_id)
    return SsoConnectionRead(
        configured=True,
        enabled=conn.enabled,
        enforced=conn.enforced,
        session_max_age_seconds=conn.session_max_age_seconds,
        protocol=conn.protocol,
        allowed_domains=",".join(await sso_domains.listed(db, conn.org_team_id)),
        oidc_issuer=conn.oidc_issuer,
        oidc_client_id=conn.oidc_client_id,
        has_client_secret=conn.oidc_client_secret_encrypted is not None,
        saml_idp_entity_id=conn.saml_entity_id,
        saml_sso_url=conn.saml_sso_url,
        has_saml_cert=conn.saml_x509_cert is not None,
        # The SP values the admin registers with their IdP (always shown).
        saml_sp_entity_id=sp.entity_id,
        saml_acs_url=sp.acs_url,
        groups_mapping=conn.groups_mapping or {},
        scim_enabled=conn.scim_enabled,
        has_scim_token=conn.scim_token_hash is not None,
        scim_base_url=sso_service.scim_base_url(),
    )


@org_router.get("", response_model=SsoConnectionRead)
async def get_sso(db: DbSession, _admin: OrgAdmin, org_id: CurrentOrg) -> SsoConnectionRead:
    conn = await sso_service.get_connection(db, org_id)
    if conn is None:
        # Surface the SP values even before first configure (the admin needs them).
        empty = await _read(db, None)
        sp = sso_service.saml_sp_config(org_id)
        return empty.model_copy(
            update={"saml_sp_entity_id": sp.entity_id, "saml_acs_url": sp.acs_url}
        )
    return await _read(db, conn)


async def _admin_session_family(db: AsyncSession, request: Request) -> UUID | None:
    """The login session the admin's request is acting from, if a browser one."""
    jti = acting_session_jti(request)
    if jti is None:
        return None
    family = await family_of_access_token(db, jti)
    return family.family_id if family is not None else None


@org_router.put("", response_model=SsoConnectionRead)
async def put_sso(
    payload: SsoConnectionUpdateRequest,
    request: Request,
    db: DbSession,
    admin: OrgAdminVerified,
    org_id: CurrentOrg,
    ctx: CurrentPrincipal,
) -> SsoConnectionRead:
    """Create or update the org's SSO connection.

    Turning ``enforced`` on requires the admin's own browser session to have
    signed in through the org's IdP within the connection's max age (otherwise
    ``409 sso_unverified``: an admin can never lock the org behind an IdP they
    have not proven works), and ends at once, in this org only, every credential
    a governed member holds that was not minted under it. Moving the connection
    to another IdP while enforced turns enforcement off; it is turned back on
    after a sign-in through the new one.

    The email domains the IdP speaks for are not set here: platform staff
    assign them (``/admin/v1/orgs/{org}/sso/domains``), and this route reads
    them back."""
    # An enforced-but-disabled connection is a contradiction (and would never take
    # effect, since discovery + enforced_login_url both gate on enabled) — reject it
    # so the stored state can't drift into a confusing combination.
    if payload.enforced and not payload.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enforced SSO requires enabled=true",
        )
    existing = await sso_service.get_connection(db, org_id)
    was_enforced = existing is not None and existing.enforced
    moved_idp = sso_service.idp_changed(
        existing,
        protocol=payload.protocol,
        oidc_issuer=payload.oidc_issuer if payload.protocol == "oidc" else None,
        oidc_client_id=payload.oidc_client_id if payload.protocol == "oidc" else None,
        saml_entity_id=payload.saml_idp_entity_id if payload.protocol == "saml" else None,
        saml_sso_url=payload.saml_sso_url if payload.protocol == "saml" else None,
        saml_x509_cert=payload.saml_x509_cert if payload.protocol == "saml" else None,
    )
    enforced = payload.enforced and not (was_enforced and moved_idp)
    if enforced and not was_enforced:
        family_id = await _admin_session_family(db, request)
        if existing is None or not await sign_in_policy.has_fresh_sso_grant(
            db, family_id=family_id, org_team_id=org_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "sso_unverified",
                    "message": "Sign in with single sign-on before enforcing it.",
                },
            )

    if payload.protocol == "saml":
        if not (payload.saml_idp_entity_id and payload.saml_sso_url):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Both saml_idp_entity_id and saml_sso_url are required for SAML",
            )
        if payload.saml_x509_cert is None and (existing is None or existing.saml_x509_cert is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An IdP certificate is required when first configuring SAML",
            )
        conn = await sso_service.upsert_saml_connection(
            db,
            org_team_id=org_id,
            idp_entity_id=payload.saml_idp_entity_id,
            sso_url=payload.saml_sso_url,
            x509_cert=payload.saml_x509_cert,
            enabled=payload.enabled,
            enforced=enforced,
            groups_mapping=dict(payload.groups_mapping),
        )
        # Leaving OIDC drops the OIDC client secret with it: a stored secret that
        # no protocol uses is a secret that can still be re-pointed at an
        # attacker's issuer later, without ever being re-entered.
        conn.oidc_client_secret_encrypted = None
        await db.flush()
        audit_detail = {
            "protocol": "saml",
            "enabled": payload.enabled,
            "enforced": enforced,
            "idp_entity_id": payload.saml_idp_entity_id,
            "groups_mapping": dict(payload.groups_mapping),
        }
    else:
        if not (payload.oidc_issuer and payload.oidc_client_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Both oidc_issuer and oidc_client_id are required for OIDC",
            )
        if not issuer_is_dialable(payload.oidc_issuer):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "oidc_issuer must be an https:// origin with no query string or "
                    "fragment, on a host this deployment may dial (Alkera's hosted "
                    "service reaches public addresses only)"
                ),
            )
        if payload.oidc_client_secret is None and (
            existing is None or existing.oidc_client_secret_encrypted is None
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A client secret is required when first configuring OIDC",
            )
        # Anti-exfiltration (the same rule BYOK provider keys carry): the stored
        # secret is POSTed to whatever endpoint the issuer's discovery document
        # names, so moving the issuer or the client id means re-entering it. Without
        # this, an org admin who cannot READ the secret can still have it delivered
        # to an IdP they control by re-pointing the issuer with the field blank.
        if payload.oidc_client_secret is None and existing is not None:
            repointed = (payload.oidc_issuer, payload.oidc_client_id) != (
                existing.oidc_issuer,
                existing.oidc_client_id,
            )
            if repointed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Changing the OIDC issuer or client ID requires re-entering "
                        "the client secret"
                    ),
                )
        conn = await sso_service.upsert_oidc_connection(
            db,
            org_team_id=org_id,
            issuer=payload.oidc_issuer,
            client_id=payload.oidc_client_id,
            client_secret=payload.oidc_client_secret,
            enabled=payload.enabled,
            enforced=enforced,
            groups_mapping=dict(payload.groups_mapping),
        )
        audit_detail = {
            "protocol": "oidc",
            "enabled": payload.enabled,
            "enforced": enforced,
            "issuer": payload.oidc_issuer,
            "groups_mapping": dict(payload.groups_mapping),
        }

    if payload.session_max_age_seconds is not None:
        conn.session_max_age_seconds = payload.session_max_age_seconds
        await db.flush()
    audit_detail["session_max_age_seconds"] = conn.session_max_age_seconds
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="sso.config_updated",
        # Never the secret/cert — only the non-sensitive shape of the change.
        detail=audit_detail,
        acting=ctx,
    )
    if was_enforced and moved_idp:
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=admin,
            action="sso.enforcement_disabled",
            detail={"reason": "idp_changed"},
            acting=ctx,
        )
    if enforced and not was_enforced:
        revoked = await sso_service.enforce_on(db, conn)
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=admin,
            action="sso.enforcement_enabled",
            detail={"memberships_revoked": revoked},
            acting=ctx,
        )
    return await _read(db, conn)


@org_router.post("/scim-token", response_model=ScimTokenResponse)
async def mint_scim_token(
    db: DbSession, admin: OrgAdminVerified, org_id: CurrentOrg
) -> ScimTokenResponse:
    """Mint (or rotate) the org's SCIM bearer token + enable SCIM. The raw token is
    shown ONCE (only its HMAC is stored); minting again rotates it — the old token
    stops working immediately.

    SCIM provisions addresses only in the domains staff assigned the org; with
    none assigned it provisions nobody."""
    _conn, raw = await sso_service.mint_scim_token(db, org_id)
    await org_audit_service.record(db, org_id=org_id, actor=admin, action="scim.token_minted")
    return ScimTokenResponse(token=raw, scim_base_url=sso_service.scim_base_url())


@org_router.delete("/scim-token", response_model=SsoConnectionRead)
async def revoke_scim_token(
    db: DbSession, admin: OrgAdminVerified, org_id: CurrentOrg
) -> SsoConnectionRead:
    """Disable SCIM provisioning and revoke the org's token."""
    conn = await sso_service.revoke_scim_token(db, org_id)
    await org_audit_service.record(db, org_id=org_id, actor=admin, action="scim.token_revoked")
    return await _read(db, conn)


@org_router.get("/exemptions", response_model=list[SsoExemptMember])
async def list_sso_exemptions(
    db: DbSession, _admin: OrgAdmin, org_id: CurrentOrg
) -> list[SsoExemptMember]:
    """Break-glass members of this org — who may sign in without the org's IdP
    even when SSO is enforced. (The bootstrap admin is exempt by default.)"""
    rows = await members_of(db, org_id, sso_exempt_only=True)
    return [SsoExemptMember(user_id=u.id, email=u.email, sso_exempt=m.sso_exempt) for u, m in rows]


@org_router.put("/exemptions/{user_id}", response_model=SsoExemptMember)
async def set_sso_exemption(
    user_id: UUID,
    payload: SsoExemptUpdateRequest,
    db: DbSession,
    admin: OrgAdminVerified,
    org_id: CurrentOrg,
    ctx: CurrentPrincipal,
) -> SsoExemptMember:
    """Grant or revoke a member's break-glass SSO exemption, on their
    membership in this org: it has no effect in any other org."""
    membership = await membership_in(db, user_id=user_id, org_team_id=org_id)
    user = await user_service.get_by_id(db, user_id) if membership is not None else None
    if membership is None or user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found in your organization"
        )
    membership.sso_exempt = payload.exempt
    await db.flush()
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="sso.exemption_changed",
        target=user.email,
        detail={"exempt": payload.exempt},
        acting=ctx,
    )
    return SsoExemptMember(user_id=user.id, email=user.email, sso_exempt=membership.sso_exempt)


__all__ = ["link_router", "org_router", "router"]
