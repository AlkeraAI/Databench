"""FastAPI dependencies for auth + permissions.

Use these in route signatures (`user: User = Depends(require_org_admin)`) or
as router-level dependencies (`APIRouter(dependencies=[Depends(...)])`).

The org of every request is its credential's org. Each door verifies the
membership the credential names (``alkera_core.auth.tenancy``) and builds the
acting context from it; ``CurrentOrg`` and ``CurrentMember`` are how a route
reads it. A client may assert the org it believes it is in
(``X-Alkera-Org``): a mismatch is a 409 before the route runs, never a
selection, a write on the browser's session cookie must make that assertion
(``backend.auth.org_assertion``), and every response that resolved a context
echoes its org.

Permission ladder:
    current_user                — any authenticated user
    require_org_admin           — Admin of the caller's root team
    require_team_admin(...)     — Admin of a specific team OR Org Admin
    require_platform_staff      — has any platform_role (SUPPORT or ADMIN)
    require_platform_admin      — platform_role == ALKERA_ADMIN
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar, cast
from uuid import UUID

from alkera_core.auth import (
    COOKIE_NAME,
    InvalidTokenError,
    MachineWorkerClaims,
    SessionClaims,
    TokenExpiredError,
    TokenRevokedError,
    assert_token_active,
    decode_machine_worker_token,
    decode_session_token,
)
from alkera_core.auth.ci_token import looks_like_ci_token
from alkera_core.auth.last_used import stamp_last_used
from alkera_core.auth.machine_credential_standing import chat_is_bound_to, owner_stands
from alkera_core.auth.machine_token import (
    looks_like_machine_token,
    looks_like_machine_worker_token,
    parse_machine_credential,
)
from alkera_core.auth.pat_token import looks_like_pat_token
from alkera_core.auth.proxy_token import looks_like_proxy_token, resolve_active_proxy_token
from alkera_core.auth.tenancy import (
    SESSION_ORG_REVOKED,
    MembershipRefused,
    require_active_membership,
    verify_claims,
)
from alkera_core.authz import (
    ActingContext,
    Action,
    AgentAssertion,
    AgentHeaderError,
    CredentialKind,
    parse_agent_assertion,
)
from alkera_core.brand import sales_email
from alkera_core.compute.machines import has_run_org_workers
from alkera_core.compute.workspace_lease import held_orgs_query, holds_work_in
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal, get_db
from alkera_core.db.tenant_session import apply_now, bind_tenant, bound_org_ids
from alkera_core.entitlements import Feature, has_feature
from alkera_core.logging import get_logger
from alkera_core.machine_refusals import (
    MACHINE_CREDENTIAL_REFUSED,
    MACHINE_WORKER_CREDENTIAL_EXPIRED,
)
from alkera_core.models import (
    AuthToken,
    OrgMembership,
    PlatformRole,
    TeamMembership,
    TeamRole,
    TokenType,
    User,
)
from alkera_core.models import CiToken as CiTokenModel
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    DEDICATED_TENANCY,
    PERSONAL_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
)
from alkera_core.models.machine_credential import MachineCredential, OrgComputeAssignment
from alkera_core.observability.context import bind_log_context
from alkera_core.org_entitlements import org_entitlements
from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.org_assertion import ORG_CHANGED_CODE, refuse_a_stale_org_assertion
from backend.auth.refusals import INVALID_SESSION, UNAUTHORIZED_CODE, Reader
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service

# Function scope commits before the response is sent; the full rationale is on
# get_db's docstring. Every injection must carry it: the dependency cache keys on
# scope, so a bare Depends(get_db) would open a SECOND, independently committed
# session beside this one.
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


#: The 401 vocabulary a client keys off. ``token_expired`` is the ONLY code that
#: invites a silent refresh-and-retry; a revoked session and every other refusal
#: hand the user back to login, so the codes must never be conflated.
TOKEN_EXPIRED_CODE = "token_expired"  # noqa: S105 — an error code, not a credential
SESSION_REVOKED_CODE = "session_revoked"
#: The credential is good but its membership in its org no longer stands
#: (deactivated, its epoch moved, or an org the single-org guard refuses).
SESSION_ORG_REVOKED_CODE = SESSION_ORG_REVOKED


log = get_logger(__name__)


def _unauthorized(
    detail: str = "not authenticated", *, code: str = UNAUTHORIZED_CODE
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": detail},
        headers={"WWW-Authenticate": "Cookie"},
    )


def _forbidden(detail: str = "insufficient permissions") -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _extract_bearer_from_headers(headers: Mapping[str, str]) -> str | None:
    """Pull a JWT out of `Authorization: Bearer <token>`. Case-insensitive
    on the scheme. Returns None if no Authorization header or wrong scheme.
    """
    header = headers.get("authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _extract_bearer(request: Request) -> str | None:
    return _extract_bearer_from_headers(request.headers)


def optional_session_claims(
    request: Request, *, allow_expired: bool = False
) -> SessionClaims | None:
    """Decode the request's session token (cookie or Bearer) without raising.

    Returns None when there's no token or it's invalid (or, unless
    ``allow_expired`` is set, expired). Used by logout, which must stay
    idempotent — a missing/dead token still clears the cookie — and which
    reads an EXPIRED token too, because it still names the session family
    that must end.
    """
    token = request.cookies.get(COOKIE_NAME) or _extract_bearer(request)
    if not token:
        return None
    try:
        return decode_session_token(token, allow_expired=allow_expired)
    except InvalidTokenError:
        return None


def _agent_assertion_or_400(request: Request) -> AgentAssertion | None:
    """The agent assertion on this request, ``None`` when the agent headers are
    absent. A half-formed or contradictory assertion is a 400, never a silent
    downgrade to "just the user"."""
    try:
        return parse_agent_assertion(request.headers)
    except AgentHeaderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_agent_headers", "message": str(exc)},
        ) from exc


def _forbid_agent_assertion(request: Request, *, credential: str) -> None:
    """Refuse the agent headers on any credential that is not a user's own
    session. Only a user can delegate to an agent; a token that claims an agent
    is forging a chain, so ANY agent header present — well-formed or not — is a
    400 rather than an ignored hint."""
    try:
        present = parse_agent_assertion(request.headers) is not None
    except AgentHeaderError:
        present = True
    if present:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "agent_actor_requires_user_session",
                "message": f"a {credential} cannot act as an agent",
            },
        )


def _stash_context(
    request: Request, ctx: ActingContext, *, browser_session: bool = False
) -> ActingContext:
    """Remember the resolved acting context for the rest of the request, bind
    it into the log context (ids only, never labels), and hold the client's
    org assertion to it. Every credential shape passes through here, so the
    request's org (``request.state.org_id``, the ``X-Alkera-Org`` echo) is
    always the credential's. ``browser_session`` says the credential is the
    browser's session cookie, whose writes must name their org."""
    request.state.acting_context = ctx
    request.state.org_id = str(ctx.org_id)
    bind_log_context(
        org_id=str(ctx.org_id),
        actor_kind=ctx.acting_principal.kind.value,
        agent_id=ctx.acting_principal.id if ctx.is_agent else "",
        machine_id=ctx.acting_principal.id if ctx.is_machine else "",
    )
    refuse_a_stale_org_assertion(request, ctx, browser_session=browser_session)
    return ctx


def tenant_org_ids(
    ctx: ActingContext, *, held_chat_orgs: frozenset[UUID] = frozenset()
) -> tuple[UUID, ...]:
    """The orgs a request acting as ``ctx`` may read and write content in: its
    own org, and for a machine every org it serves. A pool box serves "every
    org" only in the sense that it may be placed anywhere; what it may touch is
    the orgs whose chats or workspaces it holds, which the caller reads and
    passes in."""
    orgs = [ctx.org_id]
    if ctx.is_machine:
        orgs.extend(sorted(ctx.served_org_ids, key=str))
        if ctx.serves_every_org:
            orgs.extend(sorted(held_chat_orgs, key=str))
    return tuple(dict.fromkeys(orgs))


async def _hold_session_to_context(request: Request, db: AsyncSession, ctx: ActingContext) -> None:
    """Bind the request's session to the orgs ``ctx`` may act within, so every
    statement the route runs on it is held to the content tables' row-level
    policy (``alkera_core.db.tenant_session``). The one call site of
    ``bind_tenant`` for requests: every credential shape reaches it through
    ``current_user`` or ``current_principal``. Binding the same orgs twice is
    a no-op, so a route that declares both pays one stamp."""
    org_ids = getattr(request.state, "tenant_org_ids", None) or tenant_org_ids(ctx)
    if bound_org_ids(db) is not None:
        bind_tenant(db, org_ids)
        return
    bind_tenant(db, org_ids)
    await apply_now(db)


def _user_context(
    request: Request, user: User, *, org_id: UUID, credential_id: str | None
) -> ActingContext:
    """A user's own session, or an agent acting inside it when the request
    carries a well-formed agent assertion, in ``org_id`` (the credential's
    org, already verified). ``credential_id`` is the session token's ``jti``:
    which of the user's sessions this is, not just whose."""
    assertion = _agent_assertion_or_400(request)
    if assertion is None:
        return ActingContext.for_user(
            user_id=user.id, org_id=org_id, email=user.email, credential_id=credential_id
        )
    return ActingContext.for_agent(
        user_id=user.id,
        org_id=org_id,
        email=user.email,
        session_id=assertion.session_id,
        credential_id=credential_id,
    )


async def current_user(request: Request, db: DbSession) -> User:
    """Resolve the calling user from the session cookie OR an
    `Authorization: Bearer <jwt>` header (the CLI uses Bearer).

    Cookie wins when both are present, since SPA traffic is the common
    path and never carries a Bearer header.

    Re-fetches the User row (no caching) so platform_role / org membership
    revocations take effect on the next request.

    The request's org is the token's org claim, and the membership it names
    must stand: active, the token's user, and (for a token that names one)
    the membership and credential epoch it was minted at. A refusal is a 401
    ``session_org_revoked``. While multi-org is off, a token for any org but
    the user's home org is refused the same way.
    """
    cookie = request.cookies.get(COOKIE_NAME)
    token = cookie or _extract_bearer(request)
    if not token:
        raise _unauthorized("missing session cookie or bearer token")
    try:
        claims = decode_session_token(token)
    except TokenExpiredError as exc:
        raise _unauthorized("session token expired", code=TOKEN_EXPIRED_CODE) from exc
    except InvalidTokenError as exc:
        # What the decoder said goes to the log, never to the caller.
        log.info("auth.session_token_invalid", error=str(exc))
        raise INVALID_SESSION.to(Reader.of(browser_session=cookie is not None)) from exc

    # A banned account is answered exactly as a deleted one — same statement,
    # same exception — so nothing about the response says which it was. The ban
    # rides the same single ``users`` read (see ``alkera_core.bans``).
    user, banned = await user_service.get_by_id_with_ban(db, claims.user_id)
    if user is None or banned:
        raise _unauthorized("user no longer exists")
    # Deprovisioning: a deactivated account is rejected on every request, so an
    # offboarded user's still-live session stops working immediately (the durable
    # check; deactivation also revokes sessions for an instant cut-off).
    if not user.is_active:
        raise _unauthorized("account is deactivated")

    try:
        await assert_token_active(db, claims, user)
    except TokenRevokedError as exc:
        raise _unauthorized(f"session revoked: {exc}", code=SESSION_REVOKED_CODE) from exc
    try:
        await verify_claims(db, claims, user=user)
    except MembershipRefused as exc:
        raise _unauthorized(
            "your membership in this organization has ended", code=SESSION_ORG_REVOKED_CODE
        ) from exc

    # Attach identity to the request + log context so the access log, every log
    # line in this request, and any Sentry event carry who made the call. IDs
    # only — never email/PII in the log fields. The org is the credential's.
    request.state.user_id = str(user.id)
    # The row itself, for a route that admits a machine OR a user and must not
    # pay a second ``users`` read to learn which it got.
    request.state.current_user_row = user
    bind_log_context(user_id=str(user.id))
    ctx = _stash_context(
        request,
        _user_context(request, user, org_id=claims.org_team_id, credential_id=claims.jti),
        browser_session=bool(cookie),
    )
    await _hold_session_to_context(request, db, ctx)
    if ctx.is_agent:
        await _refuse_a_box_whose_credential_is_gone(request, db, ctx)
    return user


async def _refuse_a_box_whose_credential_is_gone(
    request: Request, db: AsyncSession, ctx: ActingContext
) -> None:
    """A platform box the platform took away is refused on every route, not
    only on the two that carry its machine credential.

    Most of what a box does — read the chat it publishes, append its events,
    mint its socket ticket, lease its folder — rides its session plus the agent
    assertion naming its machine, and never presents the credential. When the
    assertion is the box's OWN session (the one its machine registered with)
    and the machine's credential is no longer live, the request is refused
    with the same opaque 401 the heartbeat answers, and the refusal is on
    record; any other agent request pays one indexed read and goes on as
    whoever it is. Only an assertion that names a machine id is asked about.
    """
    from alkera_core.compute.machines import MachineStanding, machine_standing

    standing = await machine_standing(
        db,
        machine_id=ctx.acting_principal.id,
        org_id=ctx.org_id,
        operator_user_id=ctx.effective_user_id,
        credential_id=ctx.credential_id,
    )
    if standing is not MachineStanding.REFUSED:
        return
    await _enforce_machine_credential(
        request,
        db,
        ctx,
        Action.WRITE,
        {
            "is_machine": True,
            "credential_live": False,
            "machine_matches": True,
            "own_standing": False,
            "runs_org_workers": False,
            "org_reached": True,
        },
    )


CurrentUser = Annotated[User, Depends(current_user)]


def _machine_refused() -> HTTPException:
    """One answer for a credential that is revoked, rotated away, never minted
    or whose machine is gone: the box must stop presenting it, and nothing in
    the answer says which of those it was."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": MACHINE_CREDENTIAL_REFUSED, "message": "Machine credential refused"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _served_orgs(
    session: AsyncSession, credential: MachineCredential
) -> tuple[frozenset[UUID], bool]:
    """The orgs a box may act within besides its operator org: every org for a
    pool box, the one it is assigned to for a dedicated box, none before it
    has claimed a machine."""
    if credential.machine_id is None:
        return frozenset(), False
    if credential.tenancy == POOL_TENANCY:
        return frozenset(), True
    if credential.tenancy == DEDICATED_TENANCY:
        rows = await session.execute(
            select(OrgComputeAssignment.org_team_id).where(
                OrgComputeAssignment.machine_id == credential.machine_id
            )
        )
        # An org machine's box serves the org its allocation was made for,
        # which can never change.
        tenant = await session.execute(
            select(ComputeAllocation.tenant_org_id).where(
                ComputeAllocation.id == credential.machine_id,
                ComputeAllocation.org_machine_id.is_not(None),
                ComputeAllocation.tenant_org_id.is_not(None),
            )
        )
        held = [org for org in tenant.scalars().all() if org is not None]
        return frozenset([*rows.scalars().all(), *held]), False
    return frozenset(), False


async def credential_serves_org(
    session: AsyncSession, credential: MachineCredential, org_id: UUID
) -> bool:
    """Whether a box on ``credential`` may act within ``org_id`` at all: the
    operator org it was minted in, and the orgs :func:`_served_orgs` names.
    The ceiling an org-bound worker credential is minted and re-checked
    against; what the box reaches inside that org is each policy's to say."""
    if org_id == credential.org_team_id:
        return True
    served, every = await _served_orgs(session, credential)
    return every or org_id in served


async def _orgs_held_by(session: AsyncSession, machine_id: str) -> frozenset[UUID]:
    """The orgs whose chats or workspaces the machine holds: one indexed read
    per arm, on a session that is not held to any org (the question is about
    every org). A person can open and run a notebook in a workspace whose
    sandbox is on the box while none of its chats is, so the workspace a box
    reports holding counts as much as a bound chat."""
    rows = await session.execute(held_orgs_query(machine_id))
    return frozenset(rows.scalars().all())


async def machine_context(session: AsyncSession, credential: MachineCredential) -> ActingContext:
    """The acting context a live machine credential resolves to: the machine it
    holds, the operator org it was minted in, and the orgs it serves.

    The one builder of a machine's context, so the HTTP door and the socket a
    box opens on a ticket cannot describe the same credential differently.
    Reads the dedicated box's assignment; the caller has already decided the
    credential stands.
    """
    served, every = await _served_orgs(session, credential)
    return ActingContext.for_machine(
        machine_id=credential.machine_id,
        credential_id=credential.id,
        org_id=credential.org_team_id,
        label=credential.label,
        served_org_ids=served,
        serves_every_org=every,
        personal_owner_id=_personal_owner(credential),
    )


def _personal_owner(credential: MachineCredential) -> UUID | None:
    """The one person a personal box serves; ``None`` for a platform box."""
    return credential.created_by if credential.tenancy == PERSONAL_TENANCY else None


async def machine_worker_context(
    session: AsyncSession, *, credential_id: UUID, machine_id: UUID, org_id: UUID
) -> ActingContext | None:
    """The context an org-bound worker credential resolves to NOW, or
    ``None`` when the machine credential behind it no longer stands behind
    ``machine_id``, may no longer act within ``org_id``, or the machine no
    longer holds work there.

    The one builder of a worker's context, shared by the bearer door and by
    the socket and stream a worker holds on a ticket, so a held connection is
    re-bound to the same one org on every keepalive and can never come back
    as the machine itself.

    The worker credential is minted only for an org the box holds work in,
    and that is asked again here on every request and keepalive
    (:func:`holds_work_in`: a chat of the org bound to the machine, a
    workspace it holds, or a live lease it is finishing on). So the
    credential stops opening anything the moment the box loses the org's
    last chat, not when its signed lifetime runs out.
    """
    from alkera_core.auth.machine_credential_standing import live_machine_of

    held = await live_machine_of(session, credential_id)
    if held is None or held != machine_id:
        return None
    credential = await session.get(MachineCredential, credential_id)
    if credential is None or not await credential_serves_org(session, credential, org_id):
        return None
    if not await holds_work_in(session, machine_id=str(machine_id), org_id=org_id):
        return None
    return ActingContext.for_machine_worker(
        machine_id=machine_id,
        credential_id=credential.id,
        org_id=org_id,
        label=credential.label,
        personal_owner_id=_personal_owner(credential),
    )


async def machine_context_if_standing(
    session: AsyncSession,
    *,
    credential_id: UUID,
    machine_id: UUID,
    org_id: UUID,
    org_bound: bool = False,
) -> ActingContext | None:
    """The context ``credential_id`` resolves to NOW, or ``None`` when the
    credential no longer stands behind ``machine_id`` in ``org_id``.

    For the connections a box holds open on a ticket or a stream rather than
    on the bearer — the socket, the event stream — which re-ask this on every
    keepalive. Read through the one standing rule every door shares, then
    built by :func:`machine_context`, so a held connection and a fresh
    request cannot describe one credential differently. ``org_bound`` names
    a connection an org-bound worker opened: it is rebuilt by
    :func:`machine_worker_context`, bound to ``org_id`` alone.
    """
    from alkera_core.auth.machine_credential_standing import live_machine_of

    from backend.services.credentials import machine_credentials as machine_credential_service

    if org_bound:
        return await machine_worker_context(
            session, credential_id=credential_id, machine_id=machine_id, org_id=org_id
        )
    held = await live_machine_of(session, credential_id)
    if held is None or held != machine_id:
        return None
    credential = await machine_credential_service.get(session, credential_id)
    if credential is None or credential.org_team_id != org_id:
        return None
    return await machine_context(session, credential)


#: The routes a box calls on its machine's own standing — the claim, the
#: heartbeat, a worker credential's mint, the routing feed and its stream.
#: Filled by :func:`machine_own_standing`, which each such route wears.
_OWN_STANDING_ENDPOINTS: set[Callable[..., Any]] = set()
EndpointT = TypeVar("EndpointT", bound=Callable[..., Any])


def machine_own_standing(endpoint: EndpointT) -> EndpointT:
    """Mark a route as one a box calls on its machine's own standing.

    A box that runs a worker per org presents its machine credential on these
    routes and nowhere else: every other route is refused to that credential
    (each org's chats and files are reached on the org's worker credential).
    Applied beneath the router's decorator, so the function the router
    registers is the one marked."""
    _OWN_STANDING_ENDPOINTS.add(endpoint)
    return endpoint


def on_own_standing_route(request: Request) -> bool:
    """Whether the request was routed to a route marked
    :func:`machine_own_standing`."""
    return request.scope.get("endpoint") in _OWN_STANDING_ENDPOINTS


async def _enforce_machine_credential(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    action: Action,
    facts: Mapping[str, object],
) -> None:
    """Decide ``facts`` about the machine credential ``ctx`` speaks on through
    the ``compute.machine_credential`` policy, on the machine itself: the
    allow returns, the denial raises, and either is on record."""
    from alkera_core.authz import Resource, ResourceType

    from backend.authz import enforce

    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.MACHINE_CREDENTIAL, id=ctx.acting_principal.id, org_id=ctx.org_id),
        facts,
    )


async def _refuse_machine_reach(
    request: Request, session: AsyncSession, ctx: ActingContext
) -> None:
    """Refuse a machine credential presented beyond the machine's own standing
    by a box that runs a worker per org, through the machine-credential
    policy, so the refusal is on record like every other decision about the
    credential. Raises."""
    safe = request.method in ("GET", "HEAD", "OPTIONS")
    await _enforce_machine_credential(
        request,
        session,
        ctx,
        Action.READ if safe else Action.WRITE,
        {
            "is_machine": True,
            "credential_live": True,
            "machine_matches": True,
            "own_standing": False,
            "runs_org_workers": True,
            "org_reached": True,
        },
    )


async def _machine_principal(request: Request, raw: str) -> ActingContext:
    """A machine credential as the bearer: the box speaking as itself.

    Resolved and stamped in a short session of its own like every other token
    shape. The credential must be live and the machine it holds must still be
    on the plane — a released or failed row is a box that no longer exists,
    and its secret opens nothing. There is no user behind the context: the box
    holds no roles, delegates for nobody, and reaches exactly what each policy's
    machine branch says it holds.

    A box whose machine says, or ever said, it runs a worker per org reaches
    only the
    routes marked :func:`machine_own_standing` on this credential; anything
    else is refused (403, on record), so a box swapped back to a process that
    serves every org from one home cannot regain that reach.

    The agent assertion headers are tolerated on this credential only when
    they name what the box may legitimately be speaking as — its own machine
    id, or a chat bound to it — and the context does not change for them: a
    box that names a chat it does not hold is a client bug or a probe and is
    told so rather than downgraded to "just the machine".
    """
    from backend.services.credentials import machine_credentials as machine_credential_service

    if looks_like_machine_worker_token(raw):
        return await _machine_worker_principal(request, raw)
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as auth_session:
        credential = await machine_credential_service.resolve_active(auth_session, raw)
        if credential is None:
            raise _machine_refused()
        if credential.tenancy == PERSONAL_TENANCY and not await owner_stands(
            auth_session, user_id=credential.created_by, org_id=credential.org_team_id
        ):
            # A personal box stands only while its person is a member of its org.
            raise _machine_refused()
        runs_org_workers = False
        if credential.machine_id is not None:
            machine = await auth_session.get(ComputeAllocation, credential.machine_id)
            if machine is None or machine.state in COMPUTE_TERMINAL_STATES:
                raise _machine_refused()
            runs_org_workers = has_run_org_workers(machine)
        ctx = await machine_context(auth_session, credential)
        if runs_org_workers and not on_own_standing_route(request):
            await _refuse_machine_reach(request, auth_session, ctx)
        held = (
            await _orgs_held_by(auth_session, ctx.acting_principal.id)
            if ctx.serves_every_org
            else frozenset()
        )
        request.state.tenant_org_ids = tenant_org_ids(ctx, held_chat_orgs=held)
        assertion = _agent_assertion_or_400(request)
        bound = assertion is None or (
            assertion.session_id == ctx.acting_principal.id
            or await chat_is_bound_to(auth_session, assertion.session_id, ctx.acting_principal.id)
        )
        await stamp_last_used(auth_session, MachineCredential, credential.id, now)
        await auth_session.commit()
    if not bound:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "agent_actor_requires_bound_chat",
                "message": "a machine credential acts only for the chats bound to its machine",
            },
        )
    return _stash_context(request, ctx)


def _worker_claims_or_401(raw: str) -> MachineWorkerClaims:
    """The claims of a presented worker credential. An expired one is told
    apart from every other failure, so the box mints a fresh one and retries
    instead of standing down; anything else is the machine refusal."""
    try:
        return decode_machine_worker_token(raw)
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": MACHINE_WORKER_CREDENTIAL_EXPIRED,
                "message": "Worker credential expired",
            },
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InvalidTokenError as exc:
        raise _machine_refused() from exc


async def _machine_worker_principal(request: Request, raw: str) -> ActingContext:
    """An org-bound worker credential as the bearer: one org's process on a
    box, speaking as the machine within that org and nowhere else.

    The signature proves what the box's machine credential minted; whether it
    still means anything is re-asked on every request: the machine credential
    is live, holds the machine the token names, and may still act within the
    token's org. The context it resolves to serves that org alone, so every
    tenancy floor refuses the box's other orgs on this credential. The agent
    assertion headers are tolerated only for the machine itself or a chat
    bound to it IN this org.
    """
    claims = _worker_claims_or_401(raw)
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as auth_session:
        ctx = await machine_worker_context(
            auth_session,
            credential_id=claims.credential_id,
            machine_id=claims.machine_id,
            org_id=claims.org_id,
        )
        if ctx is None:
            raise _machine_refused()
        assertion = _agent_assertion_or_400(request)
        bound = assertion is None or (
            assertion.session_id == ctx.acting_principal.id
            or await chat_is_bound_to(
                auth_session, assertion.session_id, ctx.acting_principal.id, org_id=ctx.org_id
            )
        )
        await stamp_last_used(auth_session, MachineCredential, claims.credential_id, now)
        await auth_session.commit()
    if not bound:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "agent_actor_requires_bound_chat",
                "message": "a machine credential acts only for the chats bound to its machine",
            },
        )
    return _stash_context(request, ctx)


async def principal_user(request: Request, db: DbSession, ctx: CurrentPrincipal) -> User | None:
    """The user behind the request, or ``None`` when a machine is acting.

    For the few routes a box calls on its own credential and a person calls on
    theirs. Every credential that is not a machine's goes through
    ``current_user`` exactly as before — a CI token, a proxy token and a
    personal access token are still refused there — so nothing changes for
    them; the user ``current_user`` already fetched is reused rather than read
    again.
    """
    if ctx.is_machine:
        return None
    cached: User | None = getattr(request.state, "current_user_row", None)
    if cached is not None:
        return cached
    return await current_user(request, db)


BROWSER_SESSION_REQUIRED_CODE = "browser_session_required"


def _browser_session_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": BROWSER_SESSION_REQUIRED_CODE,
            "message": "Approve this in a browser where you're signed in",
        },
    )


async def require_browser_session(request: Request, db: DbSession) -> User:
    """The calling user, only when the credential is a browser sign-in.

    For a route whose whole point is a human's consent in the portal: approving
    a device grant mints a new long-lived token, so a credential that is not a
    browser session must not be able to do it — a stolen CLI token would
    otherwise launder itself into a fresh one that survives revoking the stolen
    session. The registered ``auth_tokens`` row decides, never the transport: a
    CLI token smuggled into the cookie header is still a CLI token. A CI, proxy,
    personal access or machine credential, an agent acting in the user's
    session and an unregistered token are refused alike, with a 403 that tells
    the reader where the action can be taken.
    """
    raw = None if request.cookies.get(COOKIE_NAME) else _extract_bearer(request)
    if raw and (
        looks_like_ci_token(raw)
        or looks_like_proxy_token(raw)
        or looks_like_pat_token(raw)
        or looks_like_machine_token(raw)
    ):
        raise _browser_session_required()
    if parse_machine_credential(request.headers) is not None:
        raise _browser_session_required()
    user = await current_user(request, db)
    ctx = cast(ActingContext, request.state.acting_context)
    if ctx.is_agent or ctx.credential_id is None:
        raise _browser_session_required()
    token_type = (
        await db.execute(select(AuthToken.token_type).where(AuthToken.jti == ctx.credential_id))
    ).scalar_one_or_none()
    if token_type is not TokenType.SESSION:
        raise _browser_session_required()
    return user


BrowserSessionUser = Annotated[User, Depends(require_browser_session)]


async def require_verified_or_grace(user: CurrentUser) -> User:
    """Block accounts whose email-verification grace window has lapsed.

    Applied to the product routers by the product's composition. The auth / oauth /
    health routers stay open so a blocked user can still read their state
    (`/auth/me`), resend the verification link, and log out. Verified, invited,
    and platform-staff accounts pass straight through.

    Raises 403 with the stable `email_verification_required` error code so the
    SPA (and any API client) can recognize the gate.
    """
    from alkera_core.verification import is_blocked

    if is_blocked(user):
        # Structured detail → envelope `error.code` the SPA gate keys off.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "email_verification_required",
                "message": "Please verify your email address to continue.",
            },
        )
    return user


VerifiedUser = Annotated[User, Depends(require_verified_or_grace)]


async def require_verified_or_grace_or_machine(request: Request, db: DbSession) -> None:
    """The product gate for the two routers a box calls on its own credential.

    A machine credential has no email to verify: it is resolved as the machine
    principal and let through to the route, whose own dependencies decide —
    a route that binds ``CurrentUser`` still refuses it there. EVERY other
    credential takes ``require_verified_or_grace`` exactly as before, byte for
    byte: only a bearer that looks like a machine credential is routed
    differently, so nothing changes for a session, a CI token, a proxy token or
    a personal access token on these routers.
    """
    raw = None if request.cookies.get(COOKIE_NAME) else _extract_bearer(request)
    if raw and looks_like_machine_token(raw):
        await current_principal(request, db)
        return
    await require_verified_or_grace(await current_user(request, db))


async def require_email_verified(user: CurrentUser) -> User:
    """Strict email-verification gate for DURABLE org-structure / security
    mutations — creating invitations, adding/removing members, editing org
    login (SSO) settings, creating/deleting/renaming teams.

    Contrast `require_verified_or_grace`, which lets a freshly-signed-up account
    use the product during its grace window. This gate is STRICTER: the email
    must be PROVEN (verified), so it rejects even grace-window accounts.

    Why it exists: an attacker can pre-emptively *squat* a victim's email with a
    bare password signup (which never proves the address) and, during the grace
    window, provision an org — planting a second admin or disabling the OAuth
    providers the real owner would later use to reclaim the account. The
    verified-OAuth account-claim defense (`oauth_service._claim_unverified_account`)
    evicts the squatter's credential but NOT those planted artifacts, and a
    disabled provider blocks the reclaim entirely. Requiring a proven email here
    stops the squatter from establishing any of it. Platform staff are exempt
    (provisioned without a real inbox).

    Apply as a route-level dependency alongside the admin guard, e.g.
        dependencies=[Depends(require_email_verified), Depends(require_team_admin("team_id"))]
    Both reuse the request-cached `current_user`, so stacking costs no extra DB
    fetch.
    """
    from alkera_core.verification import requires_verification

    if requires_verification(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address to perform this action.",
            },
        )
    return user


EmailVerifiedUser = Annotated[User, Depends(require_email_verified)]


async def _is_admin_of_team(
    db: AsyncSession, *, user_id: UUID, team_id: UUID, org_team_id: UUID
) -> bool:
    """Whether ``user_id`` holds an ADMIN seat on ``team_id`` in the org
    ``org_team_id`` (the request's org). A seat in any other org answers no,
    whatever team it names."""
    row = await db.execute(
        select(TeamMembership).where(
            TeamMembership.user_id == user_id,
            TeamMembership.org_team_id == org_team_id,
            TeamMembership.team_id == team_id,
            TeamMembership.role == TeamRole.ADMIN,
        )
    )
    return row.scalar_one_or_none() is not None


async def _is_admin_of_team_or_ancestor(
    db: AsyncSession, *, user_id: UUID, team_id: UUID, org_team_id: UUID
) -> bool:
    """Permission descent: admin in any ancestor (including the org root)
    grants admin on `team_id`, counting only seats in ``org_team_id``. Walks
    the chain leaf-first."""
    chain = await team_service.ancestor_chain(db, team_id)
    for team in chain:
        if await _is_admin_of_team(db, user_id=user_id, team_id=team.id, org_team_id=org_team_id):
            return True
    return False


def _acting_org(request: Request) -> UUID:
    """The org of the request's credential. Every helper below runs after
    ``current_user`` (they depend on it), which stashed the context."""
    ctx = getattr(request.state, "acting_context", None)
    if ctx is None:
        raise RuntimeError("the acting context is read before the principal was resolved")
    return cast(ActingContext, ctx).org_id


async def require_org_admin(request: Request, user: CurrentUser, db: DbSession) -> User:
    """Caller must be ADMIN of the root (top-level) team of the org their
    credential is in.

    This is the gate for every top-level self-hosted org action — internal
    billing allocation, proxy-token management, SSO/IdP config, the audit log.
    It checks an ADMIN role on the ROOT team specifically, NOT permission
    descent: a sub-team admin holds only a MEMBER row at the root (see
    `membership_service.add_member`), so they are correctly refused here.
    """
    org_id = _acting_org(request)
    if not await _is_admin_of_team(db, user_id=user.id, team_id=org_id, org_team_id=org_id):
        raise _forbidden("org admin role required")
    return user


async def require_org_admin_verified(request: Request, user: CurrentUser, db: DbSession) -> User:
    """Org-root admin AND a PROVEN (verified) email — the gate for top-level
    org SECURITY/MONEY mutations: SSO/IdP config, proxy-token mint/revoke, and
    credit issuance.

    Stacking the strict email gate on top of org-admin closes the grace-window
    squat vector: an attacker who bare-password-signs-up at a victim's address
    gets a usable session during the verification grace window, but must NOT be
    able to configure the org's SSO / mint proxy tokens / mint credit before the
    address is proven (see `require_email_verified`). In a no-email / air-gapped
    deployment the verification gate is a no-op, so legitimate self-hosted admins
    are unaffected.
    """
    await require_email_verified(user)
    return await require_org_admin(request, user, db)


async def user_is_org_admin(db: AsyncSession, user: User, *, org_team_id: UUID) -> bool:
    """Non-raising check: is `user` an ADMIN of the root team of
    ``org_team_id`` (the request's org, ``CurrentOrg``)? Used to surface
    org-admin capability to the SPA (e.g. the dashboard payload)."""
    return await _is_admin_of_team(
        db, user_id=user.id, team_id=org_team_id, org_team_id=org_team_id
    )


def require_team_admin(team_id_param: str = "team_id") -> Callable[..., Awaitable[User]]:
    """Factory: returns a dep that 403s unless the caller is Admin of the
    team identified by the given path parameter, OR Org Admin — and 404s unless
    that team lives in the CALLER's org.

    The tenancy predicate belongs here, not at each call site. Permission descent
    alone is a pure membership check: it authorizes on the presence of an ADMIN
    row anywhere up the target's ancestor chain and never compares the target's
    org root to the caller's. In a consistent data set the chain cannot cross an
    org boundary, but a single stray cross-org membership row would otherwise
    hand a foreign tenant's teams (their warehouse connections, their invitations)
    to an outsider — with the audit entry filed under the WRONG org. Enforcing it
    in the shared dependency covers every router uniformly; the routers that also
    call `team_service.belongs_to_org` are belt-and-braces.

    404 (not 403) matches the opaque-not-found convention the teams / memberships
    routers already use: a foreign team id must not be confirmed to exist.

    Usage on a route:
        @router.post("/teams/{team_id}/memberships",
                     dependencies=[Depends(require_team_admin("team_id"))])
    """

    async def _dep(
        request: Request,
        user: CurrentUser,
        db: DbSession,
        team_id: UUID = Path(..., alias=team_id_param),
    ) -> User:
        # One chain fetch answers both checks below (the chain walk scans the
        # whole team table, so it must not run twice per request).
        chain = await team_service.ancestor_chain(db, team_id)
        # A team outside the caller's org must be indistinguishable from a
        # nonexistent one. A 403 here would confirm to an outsider that the id
        # is live (the require_entitled discipline, applied to tenancy). An
        # unresolvable team yields an empty chain and lands here too.
        org_id = _acting_org(request)
        if not any(t.id == org_id for t in chain):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        # Permission descent: admin in any ancestor (including the org root)
        # grants admin perms on the leaf team. Org Admin = Admin of the root,
        # which is the top of every team's chain in their org.
        for team in chain:
            if await _is_admin_of_team(db, user_id=user.id, team_id=team.id, org_team_id=org_id):
                return user
        raise _forbidden("team admin role required")

    return _dep


async def require_platform_staff(request: Request, user: CurrentUser) -> User:
    """Floor for /admin/v1/...: ALKERA_SUPPORT or ALKERA_ADMIN.

    Stashes the resolved staff user on ``request.state.actor`` so `AuditedRoute`
    can attribute the action without re-decoding the token.
    """
    if user.platform_role is None:
        raise _forbidden("alkera staff role required")
    request.state.actor = user
    return user


async def require_platform_admin(user: CurrentUser) -> User:
    """Higher tier: ALKERA_ADMIN only. Used inside /admin/v1/... for the
    most dangerous operations (DELETE org, granting platform roles)."""
    if user.platform_role is not PlatformRole.ALKERA_ADMIN:
        raise _forbidden("alkera admin role required")
    return user


def require_entitled(feature: Feature) -> Callable[[], Coroutine[Any, Any, None]]:
    """Dependency factory gating a router/route on a signed entitlement.

    Raises a bare 404 — NOT 403 — unless this is a self-hosted install with the
    feature entitled: the gated surface must look nonexistent (SaaS orgs and
    unentitled installs are indistinguishable), never merely locked.
    """

    async def _dep() -> None:
        if not (settings.is_self_hosted and has_feature(feature)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return _dep


async def require_enterprise_features(request: Request, user: CurrentUser, db: DbSession) -> None:
    """Gate the Enterprise-only org surfaces (SSO/IdP config, the audit log).

    Always passes on a self-hosted install; on Alkera's SaaS it 403s unless the
    caller's org holds an active Enterprise plan. A 403 (not the 404 that
    `require_entitled` raises) is deliberate: these surfaces are visibly present
    on the hosted app — the SPA renders a Contact-Sales gate — so "not entitled"
    is the honest status, not "nonexistent". Apply as a router-level dependency;
    it reuses the request-cached `current_user`, so stacking it with the per-route
    org-admin guard costs no extra DB fetch.
    """
    if not await org_entitlements().enterprise_features(db, _acting_org(request)):
        sales = sales_email()
        contact = f"Contact {sales} to enable it." if sales else "Ask your administrator."
        raise _forbidden(f"This is an Enterprise feature. {contact}")


OrgAdmin = Annotated[User, Depends(require_org_admin)]
OrgAdminVerified = Annotated[User, Depends(require_org_admin_verified)]
PlatformStaff = Annotated[User, Depends(require_platform_staff)]
PlatformAdmin = Annotated[User, Depends(require_platform_admin)]


async def require_ci_token(request: Request, db: DbSession) -> CiTokenModel:
    """The CI token dependency: :func:`_resolve_ci_token`, with the request's
    session held to the token's org."""
    token = await _resolve_ci_token(request)
    await _hold_session_to_context(request, db, request.state.acting_context)
    return token


async def _resolve_ci_token(request: Request) -> CiTokenModel:
    """Authenticate a machine caller by an org/repo-scoped CI token
    (``Authorization: Bearer alk_ci_…``) — the schema gate's userless upload
    credential. Resolves to the live token row (revoked/expired rejected) and
    stamps ``last_used_at``. There is no user in this flow; org scoping comes
    from the row's ``org_team_id`` and repo scoping from its ``repo`` column
    (enforced by the routes).

    Resolve AND stamp run in one short-lived session of their own, deliberately
    NOT the request session:

    - the stamp must survive a rollback — the request-scoped session rolls back
      when the request later fails (e.g. a 403 repo-scope probe), and a stolen
      token being probed must still leave a usage trace;
    - it must not hold two pooled connections at once. Resolving on the request
      session and then opening a second session for the stamp checked out a
      second connection while the first was still held; with the pool's default
      size, enough simultaneous gate calls each waited for a connection none of
      them could release, stalling every request the process served — not just
      the gate ones — until the pool timeout fired.

    The row is returned detached (the sessionmaker sets ``expire_on_commit=False``,
    so its loaded columns survive), which is all the routes read.
    """
    from backend.services.credentials import ci_tokens as ci_token_service

    raw = _extract_bearer_from_headers(request.headers)
    if not raw or not looks_like_ci_token(raw):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CI token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as auth_session:
        token = await ci_token_service.resolve_active(auth_session, raw, now=now)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, expired, or revoked CI token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await stamp_last_used(auth_session, CiTokenModel, token.id, now)
        await auth_session.commit()
    # Stamped first, so a token probing with forged agent headers still leaves
    # its usage trace before it is refused.
    _forbid_agent_assertion(request, credential="CI token")
    _stash_context(
        request,
        ActingContext.for_service(
            token_id=token.id,
            org_id=token.org_team_id,
            label=token.label or "",
            credential=CredentialKind.CI_TOKEN,
        ),
    )
    return token


CiTokenAuth = Annotated[CiTokenModel, Depends(require_ci_token)]


async def current_principal(request: Request, db: DbSession) -> ActingContext:
    """Resolve whichever credential the request carries into one acting context.

    Six shapes: the session cookie, a Bearer user JWT (both optionally with
    the agent assertion headers, which make the caller an agent acting for that
    user), a CI token, a proxy token, a personal access token and a machine
    credential (a box speaking as itself; no user behind it). The context is
    stashed on ``request.state.acting_context``: when a route also declares
    ``CurrentUser``, that dependency has already fetched the user and stashed
    the context, so this returns it without a second ``users`` read.

    Cookie wins over a Bearer header, exactly as in ``current_user``. The token
    shapes resolve and stamp ``last_used_at`` in a short session of their own so
    the stamp survives a later rollback of the request (see ``require_ci_token``
    for why that session must also never overlap the request's connection).
    The agent headers are valid on a user JWT only; on any token they are a 400.

    Deliberately unchanged: ``current_user`` still accepts only a JWT, so no
    existing route becomes callable with a personal access token or a proxy
    token by this dependency existing — a route opts in by depending on it.
    """
    ctx = await _resolve_principal(request, db)
    await _hold_session_to_context(request, db, ctx)
    return ctx


async def _resolve_principal(request: Request, db: AsyncSession) -> ActingContext:
    cached = getattr(request.state, "acting_context", None)
    if cached is not None:
        return cast(ActingContext, cached)
    raw = None if request.cookies.get(COOKIE_NAME) else _extract_bearer(request)
    if raw and looks_like_machine_token(raw):
        return await _machine_principal(request, raw)
    if raw and looks_like_ci_token(raw):
        await _resolve_ci_token(request)
        return cast(ActingContext, request.state.acting_context)
    if raw and looks_like_proxy_token(raw):
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as auth_session:
            proxy = await resolve_active_proxy_token(auth_session, raw, now=now)
            if proxy is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid, expired, or revoked proxy token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            await stamp_last_used(auth_session, type(proxy), proxy.id, now)
            await auth_session.commit()
        _forbid_agent_assertion(request, credential="proxy token")
        return _stash_context(
            request,
            ActingContext.for_service(
                token_id=proxy.id,
                org_id=proxy.org_team_id,
                label=proxy.label or "",
                credential=CredentialKind.PROXY_TOKEN,
            ),
        )
    if raw and looks_like_pat_token(raw):
        from backend.services.credentials import pats as pat_service

        now = datetime.now(UTC)
        async with AsyncSessionLocal() as auth_session:
            try:
                resolved = await pat_service.resolve_active(auth_session, raw, now=now)
            except MembershipRefused as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": SESSION_ORG_REVOKED_CODE,
                        "message": "your membership in this organization has ended",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            if resolved is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid, expired, or revoked personal access token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            await stamp_last_used(auth_session, type(resolved.token), resolved.token.id, now)
            await auth_session.commit()
        _forbid_agent_assertion(request, credential="personal access token")
        return _stash_context(
            request,
            ActingContext.for_pat(
                token_id=resolved.token.id,
                org_id=resolved.token.org_team_id,
                label=resolved.token.label or "",
                user_id=resolved.owner.id,
                email=resolved.owner.email,
            ),
        )
    # A JWT (cookie or Bearer): current_user stashes the user or agent context.
    await current_user(request, db)
    return cast(ActingContext, request.state.acting_context)


CurrentPrincipal = Annotated[ActingContext, Depends(current_principal)]
PrincipalUser = Annotated[User | None, Depends(principal_user)]


async def current_org(ctx: CurrentPrincipal) -> UUID:
    """The request's org: its credential's, verified at the door. The one
    org a route reads; never ``user.org_team_id``, never a client field."""
    return ctx.org_id


CurrentOrg = Annotated[UUID, Depends(current_org)]


@dataclass(frozen=True)
class Member:
    """The person behind the request and their membership in its org."""

    user: User
    membership: OrgMembership
    org_id: UUID


async def current_member(request: Request, db: DbSession, ctx: CurrentPrincipal) -> Member:
    """The user the request counts as (a person on their session, an agent in
    it, a personal access token) with their active membership in the
    credential's org. A credential with no person behind it (a machine, a CI
    or proxy token) is refused with a 403; a membership that ended since the
    door checked it, with the door's 401."""
    user_id = ctx.effective_user_id
    if user_id is None:
        raise _forbidden("a member's credential is required")
    cached: User | None = getattr(request.state, "current_user_row", None)
    user = (
        cached
        if cached is not None and cached.id == user_id
        else await user_service.get_by_id(db, user_id)
    )
    try:
        membership = await require_active_membership(db, user_id=user_id, org_team_id=ctx.org_id)
    except MembershipRefused as exc:
        raise _unauthorized(
            "your membership in this organization has ended", code=SESSION_ORG_REVOKED_CODE
        ) from exc
    if user is None:
        raise _unauthorized("user no longer exists")
    return Member(user=user, membership=membership, org_id=ctx.org_id)


CurrentMember = Annotated[Member, Depends(current_member)]


__all__ = [
    "BROWSER_SESSION_REQUIRED_CODE",
    "ORG_CHANGED_CODE",
    "SESSION_ORG_REVOKED_CODE",
    "BrowserSessionUser",
    "CiTokenAuth",
    "CurrentMember",
    "CurrentOrg",
    "CurrentPrincipal",
    "CurrentUser",
    "DbSession",
    "EmailVerifiedUser",
    "Member",
    "OrgAdmin",
    "OrgAdminVerified",
    "PlatformAdmin",
    "PlatformStaff",
    "PrincipalUser",
    "VerifiedUser",
    "credential_serves_org",
    "current_member",
    "current_org",
    "current_principal",
    "current_user",
    "held_orgs_query",
    "machine_context",
    "machine_context_if_standing",
    "machine_own_standing",
    "machine_worker_context",
    "on_own_standing_route",
    "principal_user",
    "require_browser_session",
    "require_ci_token",
    "require_email_verified",
    "require_enterprise_features",
    "require_entitled",
    "require_org_admin",
    "require_org_admin_verified",
    "require_platform_admin",
    "require_platform_staff",
    "require_team_admin",
    "require_verified_or_grace",
    "user_is_org_admin",
]
