"""Gateway auth: verify the caller's Alkera JWT + revocation.

Uses a short-lived session (not the request-scoped `get_db`) so no DB connection
is pinned for the duration of a long stream. The token is the user's Alkera
session/CLI JWT; the gateway swaps in real provider credentials downstream.

The org a request bills is its credential's org, and the membership the
credential names must stand (``alkera_core.auth.tenancy``): a session or CLI
JWT's own membership, or, for a chat gateway token, the billed person's
membership in the chat's org. A gateway token minted under a box operator's
session for somebody else's chat also needs the operator's session and
membership to stand, since the token lives only as long as they do. A refusal
is a 401 ``session_org_revoked``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from alkera_core.auth import (
    GATEWAY_PARENT_MACHINE,
    GatewayTokenClaims,
    InvalidTokenError,
    SessionClaims,
    TokenRevokedError,
    assert_token_active,
    decode_gateway_credential,
)
from alkera_core.auth.last_used import stamp_last_used
from alkera_core.auth.machine_credential_standing import chat_is_bound_to, live_machine_of
from alkera_core.auth.proxy_token import looks_like_proxy_token, resolve_active_proxy_token
from alkera_core.auth.tenancy import (
    SESSION_ORG_REVOKED,
    MembershipRefused,
    assert_single_org,
    verify_membership,
)
from alkera_core.bans import banned_predicate
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ProxyToken, User
from alkera_core.verification import requires_verification
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model_gateway.extension_points import proxy_identity


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _membership_refused() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": SESSION_ORG_REVOKED,
            "message": "your membership in this organization has ended",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _credential(request: Request) -> str | None:
    """The caller's Alkera JWT, from either auth channel.

    `Authorization: Bearer` is the canonical channel (OpenAI-compatible AI SDK,
    direct clients) and wins when present. opencode's `@ai-sdk/anthropic` wire
    authenticates with the native Anthropic `x-api-key` header instead — we
    accept that as the same Alkera JWT so the anthropic/bedrock wire reaches the
    gateway. (The gateway still swaps in real provider credentials downstream.)
    """
    bearer = _bearer(request)
    if bearer:
        return bearer
    api_key = request.headers.get("x-api-key")
    if api_key and api_key.strip():
        return api_key.strip()
    return None


async def _authenticate_proxy(token: str) -> SessionClaims:
    """A self-hosted gateway in proxy mode authenticates with an org-scoped proxy
    token (not a user JWT). It resolves to a per-org machine billing identity —
    usage meters to the ORG, no customer user identity is received. The token's
    ``last_used_at`` is stamped for the operator's visibility, at most once per
    resolution window and never waiting on a concurrent request's row lock, so
    a busy self-hosted gateway's requests do not queue on their own token."""
    resolve_identity = proxy_identity()
    if resolve_identity is None:
        # No biller stands behind this gateway, so nothing funds a proxy
        # token's requests: it is not a credential here.
        raise _unauthorized("invalid or revoked proxy token")
    async with AsyncSessionLocal() as db:
        proxy = await resolve_active_proxy_token(db, token)
        if proxy is None:
            raise _unauthorized("invalid or revoked proxy token")
        machine = await resolve_identity(db, proxy)
        now = datetime.now(UTC)
        await stamp_last_used(db, ProxyToken, proxy.id, now)
        await db.commit()
        epoch = int(now.timestamp())
        return SessionClaims(
            user_id=machine.id,
            email=machine.email,
            org_team_id=proxy.org_team_id,
            platform_role=None,
            issued_at=epoch,
            expires_at=epoch + 3600,
            jti=None,
        )


def _session_of_gateway_token(credential: GatewayTokenClaims, user: User) -> SessionClaims:
    """The session shape a chat's gateway token authenticates as: the user and
    org the token names (the person billed and the org billed), the email and
    role read from the user row rather than carried in the token, the PARENT
    session's ``jti`` and issue time so revocation is decided against that
    session, the token's own expiry, and the chat it was minted for."""
    return SessionClaims(
        user_id=credential.user_id,
        email=user.email,
        org_team_id=credential.org_id,
        platform_role=user.platform_role,
        issued_at=credential.parent_issued_at,
        expires_at=credential.expires_at,
        jti=credential.parent_jti,
        chat_id=credential.chat_id,
        membership_id=credential.membership_id,
        membership_epoch=credential.membership_epoch,
    )


def _parent_session_of(credential: GatewayTokenClaims, operator: User) -> SessionClaims:
    """The operator's session a delegated gateway token rides: its user, org,
    membership, ``jti`` and issue time, so revocation and the membership
    check are decided against the session the token dies with."""
    return SessionClaims(
        user_id=operator.id,
        email=operator.email,
        org_team_id=credential.org_id,
        platform_role=operator.platform_role,
        issued_at=credential.parent_issued_at,
        expires_at=credential.expires_at,
        jti=credential.parent_jti,
        membership_id=credential.parent_membership_id,
        membership_epoch=credential.parent_membership_epoch,
    )


async def _standing_user(db: AsyncSession, user_id: UUID) -> User:
    """The user row a credential names, refused unless it may act: a banned
    account is answered exactly as a deleted one, as the API does, and a
    deactivated one is refused here even when the deactivation path skipped
    the revoke, so an offboarded person never keeps spending their org's
    credits until a token expires."""
    row = (await db.execute(select(User, banned_predicate()).where(User.id == user_id))).first()
    user: User | None = row[0] if row is not None else None
    if user is None or (row is not None and bool(row[1])):
        raise _unauthorized("user no longer exists")
    if not user.is_active:
        raise _unauthorized("account is deactivated")
    return user


async def authenticate(request: Request) -> SessionClaims:
    token = _credential(request)
    if not token:
        raise _unauthorized("missing bearer token")
    # An org-scoped proxy token (greppable prefix) routes to the proxy path before
    # any JWT decode — it isn't a JWT and would always fail to decode.
    if looks_like_proxy_token(token):
        return await _authenticate_proxy(token)
    try:
        credential = decode_gateway_credential(token)
    except InvalidTokenError as exc:
        raise _unauthorized(f"invalid token: {exc}") from exc

    async with AsyncSessionLocal() as db:
        # The person billed: the session's own user, or the one a chat's
        # gateway token names.
        user = await _standing_user(db, credential.user_id)
        # Whose session the credential rides, and the claims revocation is
        # decided against. None for a token under a box's machine credential,
        # which rides no session.
        holder: User | None = user
        parent: SessionClaims | None
        # The box a machine-parented token was minted under; None otherwise.
        machine: UUID | None = None
        if isinstance(credential, GatewayTokenClaims):
            # A chat's gateway token authenticates as the user it names, so the
            # funding chain, the meters and the entitlements are that person's,
            # and it never slides an idle window: an agent's calls must not keep
            # a person's session alive.
            claims = _session_of_gateway_token(credential, user)
            slide_idle = False
            if credential.parent_kind == GATEWAY_PARENT_MACHINE:
                # Minted under a box's machine credential for the chat's
                # owner: it stands while the credential does, revoked with it,
                # ended with its machine, and no session bears on it. The
                # owner's own standing (an active, verified account) still does.
                machine = await live_machine_of(db, credential.parent_jti)
                if machine is None:
                    raise _unauthorized("machine credential revoked")
                holder, parent = None, None
            elif credential.parent_user_id is not None:
                # Minted under a box operator's session for somebody else's
                # chat: it bills the person it names and dies with the
                # operator's session and membership, so both are asked.
                holder = await _standing_user(db, credential.parent_user_id)
                parent = _parent_session_of(credential, holder)
            else:
                # Under its biller's own session: revoked with that session.
                parent = claims
        else:
            claims = credential
            parent = credential
            slide_idle = True
        try:
            # The single-org guard, on the identity whose session the
            # credential rides. A machine-parent token bills the chat's owner
            # in the chat's org, which may not be the owner's home org.
            if holder is not None and parent is not None:
                assert_single_org(holder, org_team_id=parent.org_team_id)
                if parent is not claims:
                    await verify_membership(
                        db,
                        user_id=parent.user_id,
                        org_team_id=parent.org_team_id,
                        membership_id=parent.membership_id,
                        membership_epoch=parent.membership_epoch,
                    )
            # The credential's org is the org billed; the billed person's
            # membership there must stand.
            await verify_membership(
                db,
                user_id=user.id,
                org_team_id=claims.org_team_id,
                membership_id=claims.membership_id,
                membership_epoch=claims.membership_epoch,
            )
        except MembershipRefused as exc:
            raise _membership_refused() from exc
        # A machine-parented token stands only while its box still holds the
        # chat it was minted for, in the org it bills: a pool box the chat
        # moved off, or whose chat was deleted, spends nobody's credit with a
        # token it was handed before. Asked after the memberships so a
        # refused membership is answered as one.
        if (
            machine is not None
            and isinstance(credential, GatewayTokenClaims)
            and not await chat_is_bound_to(
                db, str(credential.chat_id), str(machine), org_id=credential.org_id
            )
        ):
            raise _unauthorized("machine no longer holds the chat")
        if holder is not None and parent is not None:
            try:
                await assert_token_active(db, parent, holder, slide_idle=slide_idle)
            except TokenRevokedError as exc:
                raise _unauthorized(f"session revoked: {exc}") from exc
        # Email-verification gate: paid inference REQUIRES a proven email — the
        # signup grace window that lets a fresh account browse the product does
        # NOT extend to spending credits here, so the gateway gates strictly
        # (same predicate as the backend's durable-mutation gate; platform staff
        # and no-email deployments are exempt via the shared policy). Both the
        # person billed and the person whose session spends must have one.
        if requires_verification(user) or (holder is not None and requires_verification(holder)):
            raise _forbidden("email_verification_required")
    return claims


GatewayAuth = Annotated[SessionClaims, Depends(authenticate)]


async def require_live_proxy_token(request: Request) -> str:
    """Authenticate an ORG PROXY TOKEN specifically (not a user JWT) — for the
    gateway-to-gateway control plane (the self-hosted heartbeat). Verifies the token
    is live and returns the raw secret so the handler can re-resolve it inside its own
    write session. 401s on a missing / non-proxy / revoked / expired token."""
    token = _credential(request)
    if not token or not looks_like_proxy_token(token):
        raise _unauthorized("proxy token required")
    async with AsyncSessionLocal() as db:
        if await resolve_active_proxy_token(db, token) is None:
            raise _unauthorized("invalid or revoked proxy token")
    return token


LiveProxyToken = Annotated[str, Depends(require_live_proxy_token)]
