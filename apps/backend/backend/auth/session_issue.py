"""Issue a browser session: the access token + the refresh token, as a pair.

Every route that signs a browser in — login, signup, the OAuth and SSO
callbacks — goes through :func:`issue_session`, and a change that ends every
session but keeps the acting tab (a password change, "sign out everywhere
else") through :func:`reissue_session`, so the two cookies are always set
together, with the same flags, and the refresh row always names the access
token minted beside it. The refresh route rotates an existing family through
the same helper.

Both mint through ``mint_for_membership``: the access token names the org the
family is in (``active_org_team_id``), and a new family records how it
authenticated (``auth_session_org_grants``), which an org's sign-in policy
reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from uuid import UUID, uuid4

from alkera_core.auth import (
    MintedRefresh,
    SessionClaims,
    bind_access,
    family_of_access_token,
    mint_refresh_token,
    register_token,
    sign_in_policy,
)
from alkera_core.auth.refresh import active_org_of
from alkera_core.auth.tenancy import MembershipRefused, home_org_id
from alkera_core.models import AuthRefreshToken, AuthSessionOrgGrant, TokenType, User
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import optional_session_claims
from backend.auth.membership_tokens import mint_for_membership
from backend.services.audit import ClientHint, record_security_event
from backend.utils.cookies import (
    clear_refresh_cookie,
    clear_session_cookie,
    set_refresh_cookie,
    set_session_cookie,
)

#: How a login session authenticated, as ``auth_session_org_grants`` records
#: it. ``password`` and ``sso`` (and an OAuth provider's own key, ``google`` or
#: ``github``) are sign-ins an org's policy weighs. The rest mark a session the
#: server issued inside a browser that had just proved itself (a signup, a
#: password or email change, "sign out everywhere else"); a re-issue carries
#: the grants of the session it replaces.
METHOD_PASSWORD = "password"  # noqa: S105 — a sign-in method name, not a credential
METHOD_SSO = "sso"
METHOD_SIGNUP = "signup"
METHOD_PASSWORD_CHANGE = "password_change"  # noqa: S105 — a method name, not a credential
METHOD_PROFILE = "profile"
METHOD_RENEWAL = "renewal"
_GRANT_METHOD_MAX = 16


def client_hint(request: Request) -> ClientHint:
    """The caller's user agent and a coarse network prefix (a /24 or /48), so a
    person can tell "my laptop at the office" from "somewhere else" without
    the list storing a precise address."""
    forwarded = request.headers.get("x-forwarded-for")
    raw = forwarded.split(",")[0].strip() if forwarded else None
    if not raw and request.client:
        raw = request.client.host
    prefix: str | None = None
    if raw:
        try:
            addr = ip_address(raw)
            bits = 24 if addr.version == 4 else 48
            prefix = str(ip_network(f"{addr}/{bits}", strict=False))
        except ValueError:
            prefix = None
    agent = request.headers.get("user-agent")
    return ClientHint(user_agent=agent[:255] if agent else None, ip_prefix=prefix)


def acting_session_jti(request: Request) -> str | None:
    """The ``jti`` of the session the request authenticated with, when the
    principal dependency resolved one; the session a re-issue replaces."""
    ctx = getattr(request.state, "acting_context", None)
    return ctx.credential_id if ctx is not None else None


async def record_grant(
    db: AsyncSession, *, family_id: UUID, org_team_id: UUID | None, method: str, at: datetime
) -> None:
    """Record (or refresh) one grant on a family: a sign-in made at ``at``.
    A repeat of the same (org, method) moves its time forward, which is what a
    step-up through an org's IdP is."""
    if not method or len(method) > _GRANT_METHOD_MAX:
        raise ValueError(f"sign-in method {method!r} is not a grant method")
    stmt = pg_insert(AuthSessionOrgGrant).values(
        id=uuid4(), family_id=family_id, org_team_id=org_team_id, method=method, authenticated_at=at
    )
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_auth_session_org_grants",
            set_={"authenticated_at": stmt.excluded.authenticated_at},
        )
    )
    await db.flush()


async def _record_grants(
    db: AsyncSession,
    *,
    family_id: UUID,
    method: str,
    org_team_id: UUID,
    previous_access_jti: str | None,
    at: datetime,
) -> None:
    """Write the grant for ``method`` on a new family, plus the grants of the
    family it replaces, at their original times: a re-issue is not a fresh
    authentication. Every method is an identity-level grant except an SSO
    sign-in, which an org's IdP made and which counts for that org alone."""
    if not method or len(method) > _GRANT_METHOD_MAX:
        raise ValueError(f"sign-in method {method!r} is not a grant method")
    own_org = org_team_id if method == METHOD_SSO else None
    grants: dict[tuple[UUID | None, str], datetime] = {(own_org, method): at}
    if previous_access_jti is not None:
        previous = await family_of_access_token(db, previous_access_jti)
        if previous is not None:
            inherited = await db.execute(
                select(AuthSessionOrgGrant).where(
                    AuthSessionOrgGrant.family_id == previous.family_id
                )
            )
            for grant in inherited.scalars():
                grants.setdefault((grant.org_team_id, grant.method), grant.authenticated_at)
    await db.execute(
        pg_insert(AuthSessionOrgGrant)
        .values(
            [
                {
                    "id": uuid4(),
                    "family_id": family_id,
                    "org_team_id": org,
                    "method": name,
                    "authenticated_at": when,
                }
                for (org, name), when in grants.items()
            ]
        )
        .on_conflict_do_nothing(constraint="uq_auth_session_org_grants")
    )


async def issue_session(
    db: AsyncSession,
    user: User,
    *,
    request: Request,
    response: Response,
    method: str,
    org_team_id: UUID | None = None,
    previous_access_jti: str | None = None,
) -> SessionClaims:
    """Start a new session family for ``user``: mint the access token for the
    membership in ``org_team_id`` (None: the home org) and the first refresh
    token, register both, record how the family authenticated (``method``),
    and set both cookies on ``response``.

    ``previous_access_jti`` names the session this one replaces in the same
    browser; its grants carry over. A re-issue after a change that ended every
    other session goes through :func:`reissue_session`, which asks the org's
    sign-in policy first. Raises :class:`~fastapi.HTTPException` (403, the
    membership refusal's code) when the membership does not stand."""
    try:
        claims = await _mint_session(
            db,
            user,
            request=request,
            response=response,
            method=method,
            org_team_id=org_team_id or home_org_id(user),
            previous_access_jti=previous_access_jti,
        )
    except MembershipRefused as exc:
        # The identity authenticated, but its membership in the org does not
        # stand: nothing is signed in, and the answer says which refusal it is.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": "You're no longer a member of this organization."},
        ) from exc
    await record_security_event(
        db,
        user_id=user.id,
        event="auth.signed_in",
        org_team_id=claims.org_team_id,
        client=client_hint(request),
        detail={"method": method},
    )
    return claims


async def reissue_session(
    db: AsyncSession,
    user: User,
    *,
    request: Request,
    response: Response,
    method: str,
    org_team_id: UUID,
) -> SessionClaims | None:
    """Keep the browser that asked signed in after a change that ended every
    session of ``user`` ("sign out everywhere else", a password or email
    change): a new family in ``org_team_id`` (the org the request is in),
    carrying the grants of the session it replaces.

    The org's sign-in policy is asked about the replaced session first, exactly
    as a refresh would ask it, so a re-issue never admits a session to an org
    the session could not have entered. When the policy or the membership
    refuses, both cookies are cleared and None comes back: the browser signs in
    again, and the change that ended the other sessions stands. Never raises a
    refusal, so a caller's revocation is never rolled back by one."""
    previous_jti = acting_session_jti(request)
    previous = await family_of_access_token(db, previous_jti) if previous_jti else None
    policy = await sign_in_policy.evaluate(
        db,
        user=user,
        org_team_id=org_team_id,
        family_id=previous.family_id if previous is not None else None,
        method=None,
    )
    if isinstance(policy, sign_in_policy.StepUp):
        _clear_cookies(response)
        return None
    try:
        async with db.begin_nested():
            return await _mint_session(
                db,
                user,
                request=request,
                response=response,
                method=method,
                org_team_id=org_team_id,
                previous_access_jti=previous_jti,
            )
    except MembershipRefused:
        _clear_cookies(response)
        return None


def _clear_cookies(response: Response) -> None:
    clear_session_cookie(response)
    clear_refresh_cookie(response)


async def _mint_session(
    db: AsyncSession,
    user: User,
    *,
    request: Request,
    response: Response,
    method: str,
    org_team_id: UUID,
    previous_access_jti: str | None,
) -> SessionClaims:
    """Mint the access + refresh pair for the membership in ``org_team_id``.
    Raises :class:`MembershipRefused` when the membership does not stand."""
    hint = client_hint(request)
    token, claims = await mint_for_membership(db, user, org_team_id, kind="session")
    await register_token(db, claims=claims, token_type=TokenType.SESSION)
    minted = await mint_refresh_token(
        db,
        user_id=user.id,
        access_jti=claims.jti,
        user_agent=hint.user_agent,
        ip_prefix=hint.ip_prefix,
        active_org_team_id=org_team_id,
    )
    await _record_grants(
        db,
        family_id=minted.row.family_id,
        method=method,
        org_team_id=org_team_id,
        previous_access_jti=previous_access_jti,
        at=minted.row.created_at,
    )
    set_session_cookie(response, token)
    set_refresh_cookie(response, minted.raw)
    return claims


async def stepped_up_family(
    db: AsyncSession, request: Request, user: User
) -> AuthRefreshToken | None:
    """The live login session this browser is already in, when it belongs to
    ``user``: the family named by the request's access token (expired or
    revoked is fine, it only names the family), with a refresh token still
    waiting to be used. None when there is no such session, so the caller
    starts a new one instead.

    An SSO sign-in that finds one writes its grant onto it (a step-up) rather
    than starting a second session in the same browser."""
    claims = optional_session_claims(request, allow_expired=True)
    if claims is None or claims.jti is None or claims.user_id != user.id:
        return None
    named = await family_of_access_token(db, claims.jti)
    if named is None:
        return None
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(AuthRefreshToken)
            .where(
                AuthRefreshToken.family_id == named.family_id,
                AuthRefreshToken.user_id == user.id,
                AuthRefreshToken.used_at.is_(None),
                AuthRefreshToken.revoked_at.is_(None),
                AuthRefreshToken.idle_expires_at > now,
                AuthRefreshToken.absolute_expires_at > now,
            )
            .order_by(AuthRefreshToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def issue_stepped_up_session(
    db: AsyncSession,
    user: User,
    family: AuthRefreshToken,
    *,
    org_team_id: UUID,
    response: Response,
) -> SessionClaims:
    """Finish a step-up on an existing family: point the family at
    ``org_team_id``, mint the access token for that membership, bind it to the
    family's waiting refresh token, and set the session cookie. The refresh
    cookie the browser holds stays good."""
    try:
        token, claims = await mint_for_membership(db, user, org_team_id, kind="session")
    except MembershipRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": "You're no longer a member of this organization."},
        ) from exc
    await register_token(db, claims=claims, token_type=TokenType.SESSION)
    await db.execute(
        update(AuthRefreshToken)
        .where(AuthRefreshToken.family_id == family.family_id)
        .values(active_org_team_id=org_team_id)
        .execution_options(synchronize_session=False)
    )
    family.active_org_team_id = org_team_id
    await bind_access(db, family, claims)
    set_session_cookie(response, token)
    return claims


async def issue_rotated_session(
    db: AsyncSession,
    user: User,
    minted: MintedRefresh,
    *,
    response: Response,
) -> SessionClaims:
    """Finish a refresh: mint the access token for the family's active org
    (the rotation has already checked the membership there), bind the two,
    and set both cookies."""
    token, claims = await mint_for_membership(
        db, user, active_org_of(minted.row, user), kind="session"
    )
    await register_token(db, claims=claims, token_type=TokenType.SESSION)
    await bind_access(db, minted.row, claims)
    set_session_cookie(response, token)
    set_refresh_cookie(response, minted.raw)
    return claims
