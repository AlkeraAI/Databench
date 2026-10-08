"""The org's workspace machine: register, heartbeat, current.

- ``POST /api/v1/machines/register`` — the daemon on a running pod registers it
  as the org's workspace machine (idempotent by pod id; the org comes from the
  principal, never the body). ``201`` when created, ``200`` when it already was.
  Every chat in the org is placed on this box, so the operator it runs as must
  be an ADMIN of the org root: a member who could register one would receive
  their colleagues' prompts, attachments and leased connection credentials.
  A member is refused ``403 org_admin_required``.
- ``POST /api/v1/machines/claim`` — a PLATFORM box (pool or dedicated) claims
  the machine its credential was minted for. The box signs in as its box user
  like any other and carries the credential beside the session in the
  ``X-Alkera-Machine-Credential`` header; the credential decides what the box
  is (kind, size, tenancy) and the ``compute.machine_credential`` policy
  decides whether this box may claim it. ``201`` when created, ``200`` when
  the pod was already registered.
- ``POST /api/v1/machines/{id}/heartbeat`` — the daemon's liveness stamp; ``204``.
  The optional body carries the box's capacity, the chats it holds and its
  daemon version — how a pool is spread. A beat from a daemon process other
  than the one that registered last is ``409 machine_stale_instance`` and
  changes nothing.
- ``GET  /api/v1/machines/current`` — what the machine banner shows.
- ``POST /api/v1/machines/me/worker-credentials`` — a box on its own machine
  credential mints a short-lived credential bound to ONE org, for the process
  that serves that org's chats. ``201``. Refused as not-found for an org with
  no live chat bound to the machine, and for a caller that is itself a worker
  credential.
- ``GET  /api/v1/machines/me/routing`` — the chats bound to the box's machine,
  as ``(chat, org, workspace, state)`` ids and states: what the box's root
  process routes to each org's process, with nothing of what any chat says.

The daemon calls these with the customer's user JWT plus the agent assertion
headers, so every route depends on ``CurrentPrincipal`` (an agent acting for
the user is accepted; the audit chain carries it) and decides through the
``compute.machine`` policy. A refused registration answers ``402`` / ``429``
with ``{code, message}`` after the refusal was put on record.

A platform box may instead present its machine credential AS the bearer on
``/claim`` and on its own heartbeat: the request then has no user behind it,
the credential decides what the box is, and the policies' machine branches
admit it for exactly the machine the credential holds. Every other credential
shape reaches these two routes exactly as before.

The claim, the heartbeat, the mint and the routing feed are the machine's own
standing (:func:`~backend.auth.dependencies.machine_own_standing`): the only
routes a box that runs a worker per org may call on its machine credential.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.auth import mint_machine_worker_token
from alkera_core.auth.machine_token import parse_machine_credential
from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.compute.machines import has_run_org_workers
from alkera_core.logging import get_logger
from alkera_core.models import MachineCredential, User
from alkera_core.models.compute import (
    ORG_TENANCY,
    PERSONAL_TENANCY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.observability.envelope import ErrorEnvelope
from alkera_core.schemas.compute import (
    MachineClaimRequest,
    MachineHeartbeatRequest,
    MachineHeartbeatResponse,
    MachineRead,
    MachineRegisterRequest,
    MachineStateRead,
)
from alkera_core.schemas.machine_routing import (
    MachineRoutingRead,
    MachineWorkerCredentialRead,
    MachineWorkerCredentialRequest,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from backend.api.deps.compute import refused
from backend.api.deps.machine_standing import decide_machine_standing, org_held_by
from backend.api.params import PathId
from backend.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    PrincipalUser,
    machine_own_standing,
)
from backend.authz import (
    DecisionSink,
    OutboxDecisionSink,
    SettledRepeatSink,
    enforce,
    role_resolver,
)
from backend.services import compute, sharing, workspaces
from backend.services.chats import routing_page
from backend.services.compute import grants
from backend.services.compute import machines as machine_service
from backend.services.compute import service as compute_service
from backend.services.credentials import machine_credentials as machine_credential_service

router = APIRouter(prefix="/api/v1/machines", tags=["machines"])
log = get_logger(__name__)

#: A claim the machine service refused (the credential was claimed by another
#: box, its machine was released, or it names nobody the box serves). One
#: answer for all of them: the reason is logged, never sent.
MACHINE_CLAIM_REFUSED_CODE = "machine_claim_refused"
MACHINE_CLAIM_REFUSED_MESSAGE = "This machine credential can't claim this machine."

_REFUSAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    402: {"model": ErrorEnvelope, "description": "Insufficient credit for the first minute"},
    429: {"model": ErrorEnvelope, "description": "The compute grant's ceiling is in use"},
}

WORKSPACE = "workspace"


def _not_found(what: str = "Machine") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


#: The heartbeat's decision sink. A daemon beats every fifteen seconds for the
#: whole life of its box, and each beat is authorised twice -- once on the
#: credential it presents, once on the machine it names. Recorded raw that is
#: roughly a quarter of a million rows per box per month, all of them the same
#: allow. The sink keeps the first of each identical run and drops its repeats
#: for an hour; a denial, or an allow whose facts have changed, is always
#: recorded. Nothing but the heartbeat is routed through it.
_HEARTBEAT_DECISIONS = SettledRepeatSink(OutboxDecisionSink(), window=timedelta(hours=1))


def heartbeat_decision_sink() -> SettledRepeatSink:
    """The sink above. Exposed so a test can clear its memory between cases."""
    return _HEARTBEAT_DECISIONS


def _own_standing_facts(alloc: ComputeAllocation | None) -> dict[str, bool]:
    """The machine-credential facts every request about the machine itself
    carries: it is the machine's own standing (which a box keeps whether or
    not it runs a worker per org), about the machine's own org."""
    return {
        "own_standing": True,
        "runs_org_workers": alloc is not None and has_run_org_workers(alloc),
        "org_reached": True,
    }


async def _decide(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    *,
    action: Action,
    resource_id: str,
    is_owner: bool,
    controls: bool = False,
    sink: DecisionSink | None = None,
) -> None:
    roles = await role_resolver(request, db, ctx).for_team(ctx.org_id)
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.COMPUTE_MACHINE, id=resource_id, org_id=ctx.org_id),
        {
            "in_org": roles.in_org,
            "roles": roles.roles,
            "email_verified": user.email_verified_at is not None,
            "is_owner": is_owner,
            "lifecycle": WORKSPACE,
            "controls": controls,
        },
        sink=sink,
    )


@router.post(
    "/register",
    response_model=MachineRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": MachineRead, "description": "Already registered"},
        **_REFUSAL_RESPONSES,
    },
)
async def register_machine(
    request: Request,
    response: Response,
    body: MachineRegisterRequest,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> MachineRead:
    mt = await compute_service.get_machine_type_by_code(
        db, provider=body.provider, code=body.machine_type_code
    )
    if mt is None or not mt.active:
        raise _not_found("Machine type")
    await _decide(
        request,
        db,
        ctx,
        user,
        action=Action.WRITE,
        resource_id=body.provider_pod_id,
        is_owner=True,
        controls=True,
    )
    try:
        alloc, created = await machine_service.register_machine(
            db,
            ctx=ctx,
            user=user,
            machine_type=mt,
            provider_pod_id=body.provider_pod_id,
            name=body.name,
            daemon_instance_id=body.daemon_instance_id,
        )
    except grants.ComputeRefusedError as exc:
        raise refused(exc) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return machine_service.machine_read(alloc, mt)


async def _presented_credential(request: Request, db: DbSession) -> MachineCredential | None:
    """The live machine credential the request carries, or ``None``."""
    raw = parse_machine_credential(request.headers)
    if raw is None:
        return None
    return await machine_credential_service.resolve_active(db, raw)


async def _credential_for(
    request: Request, db: DbSession, ctx: CurrentPrincipal
) -> MachineCredential | None:
    """The live credential this request acts on: the bearer itself when a
    machine is acting (re-read in the request session, so a revoke that landed
    since authentication is seen), else the one carried beside a session."""
    if not ctx.is_machine:
        return await _presented_credential(request, db)
    credential = await machine_credential_service.get(db, UUID(ctx.credential_id or ""))
    if credential is None or credential.revoked_at is not None:
        return None
    return credential


def _person(user: User | None) -> User:
    """The user behind a request that is not a machine's. ``principal_user``
    answers ``None`` only for a machine, so a route that already took its
    machine branch never reaches this."""
    if user is None:
        raise _not_found()
    return user


@router.post(
    "/claim",
    response_model=MachineRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": MachineRead, "description": "Already registered"}},
)
@machine_own_standing
async def claim_machine(
    request: Request,
    response: Response,
    body: MachineClaimRequest,
    user: PrincipalUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> MachineRead:
    """A platform box registers as the machine its credential names.

    Decided by ``compute.machine_credential``: a request without a live
    credential is refused (the header may be absent, or name a revoked one),
    and a credential already claimed by a different pod is an opaque not-found
    — a box that learned another box's id does not become it. The credential
    is the bearer itself when the box speaks on it, or rides beside the box
    user's session in its own header.
    """
    credential = await _credential_for(request, db, ctx)
    machine_type = (
        await db.get(ComputeMachineType, credential.machine_type_id)
        if credential is not None
        else None
    )
    existing = (
        await machine_service.find_registered(
            db,
            org_id=credential.org_team_id,
            provider=machine_type.provider,
            provider_pod_id=body.provider_pod_id,
        )
        if credential is not None and machine_type is not None
        else None
    )
    matches = (
        credential is not None
        and (
            credential.machine_id is None
            or (existing is not None and credential.machine_id == existing.id)
        )
        and _personal_claim_is_its_own(credential, existing)
    )
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        Resource(ResourceType.MACHINE_CREDENTIAL, id=body.provider_pod_id, org_id=ctx.org_id),
        {
            "is_machine": ctx.is_machine or parse_machine_credential(request.headers) is not None,
            "credential_live": credential is not None,
            "machine_matches": matches,
            **_own_standing_facts(existing),
        },
    )
    if credential is None or machine_type is None:
        # enforce() refused before this line for every such case; the check
        # keeps the types honest for the call below.
        raise _not_found("Machine credential")
    try:
        alloc, created = await machine_service.register_platform_machine(
            db,
            ctx=ctx,
            user=user,
            credential=credential,
            machine_type=machine_type,
            provider_pod_id=body.provider_pod_id,
            name=body.name,
            capacity=body.capacity,
            daemon_version=body.daemon_version,
            daemon_instance_id=body.daemon_instance_id,
            sandbox=body.sandbox,
        )
    except machine_service.BoxTooOldError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        # The service's reason names internal state; the box is told only that
        # its claim was refused, and the reason is kept for whoever reads logs.
        log.warning(
            "machines.claim.refused",
            reason=str(exc),
            credential_id=str(credential.id),
            machine_id=str(credential.machine_id) if credential.machine_id else "",
            provider_pod_id=body.provider_pod_id,
            org_id=str(credential.org_team_id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": MACHINE_CLAIM_REFUSED_CODE, "message": MACHINE_CLAIM_REFUSED_MESSAGE},
        ) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    read = machine_service.machine_read(alloc, machine_type)
    read.card = await machine_service.box_card_for(db, alloc, org_id=credential.org_team_id)
    return read


def _personal_claim_is_its_own(
    credential: MachineCredential, existing: ComputeAllocation | None
) -> bool:
    """A personal box's credential may claim a new row, or a personal box its
    own person already holds, and nothing else: a member who names another
    box's pod id must not turn a colleague's box, or the org's, into theirs."""
    if credential.tenancy != PERSONAL_TENANCY or existing is None:
        return True
    return existing.tenancy == PERSONAL_TENANCY and existing.user_id == credential.created_by


@router.post(
    "/{machine_id}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        200: {
            "model": MachineHeartbeatResponse,
            "description": "The beat was taken; the box backs an org machine and is told about it",
        },
        409: {"description": "The beat came from a daemon process that is not current"},
    },
)
@machine_own_standing
async def heartbeat(
    request: Request,
    machine_id: PathId,
    user: PrincipalUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    body: MachineHeartbeatRequest | None = None,
) -> Response:
    try:
        parsed = UUID(machine_id)
    except ValueError as exc:
        raise _not_found() from exc
    if ctx.is_machine:
        alloc = await _machine_beats_its_own_row(request, db, ctx, parsed)
        return await _beat(db, ctx, alloc, body)
    person = _person(user)
    alloc = await machine_service.get_workspace_machine(db, machine_id=parsed, org_id=ctx.org_id)
    presented = parse_machine_credential(request.headers) is not None
    if presented or (alloc is not None and alloc.tenancy != ORG_TENANCY):
        # A platform box re-presents its credential on every beat, so the beat
        # after an admin revokes it is refused: the box stops taking chats and
        # hands its folders back, instead of serving on until something else
        # notices. A beat for a platform machine that leaves the credential off
        # is refused the same way — otherwise a box could keep its standing by
        # never sending it. Same policy the claim runs.
        credential = await _presented_credential(request, db) if presented else None
        await enforce(
            request,
            db,
            ctx,
            Action.WRITE,
            Resource(ResourceType.MACHINE_CREDENTIAL, id=machine_id, org_id=ctx.org_id),
            {
                "is_machine": presented,
                "credential_live": credential is not None,
                "machine_matches": credential is not None
                and alloc is not None
                and credential.machine_id == alloc.id,
                **_own_standing_facts(alloc),
            },
            sink=_HEARTBEAT_DECISIONS,
        )
    await _decide(
        request,
        db,
        ctx,
        person,
        action=Action.WRITE,
        resource_id=machine_id,
        is_owner=alloc is not None and alloc.user_id == person.id,
        sink=_HEARTBEAT_DECISIONS,
    )
    return await _beat(db, ctx, alloc, body)


async def _machine_beats_its_own_row(
    request: Request, db: DbSession, ctx: CurrentPrincipal, machine_id: UUID
) -> ComputeAllocation | None:
    """A box on its own credential may beat for the one machine the credential
    holds. Both policies the session path runs are run here too — the
    credential's, with the credential the bearer proved live, and the
    machine's, on its machine branch — so the same two rows are on record for
    a beat whichever way the box authenticated."""
    alloc = await machine_service.get_workspace_machine(
        db, machine_id=machine_id, org_id=ctx.org_id
    )
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        Resource(ResourceType.MACHINE_CREDENTIAL, id=str(machine_id), org_id=ctx.org_id),
        {
            "is_machine": True,
            "credential_live": True,
            "machine_matches": alloc is not None and str(alloc.id) == ctx.acting_principal.id,
            **_own_standing_facts(alloc),
        },
        sink=_HEARTBEAT_DECISIONS,
    )
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        Resource(ResourceType.COMPUTE_MACHINE, id=str(machine_id), org_id=ctx.org_id),
        {
            "in_org": True,
            "roles": frozenset(),
            "email_verified": False,
            "is_owner": False,
            "lifecycle": WORKSPACE,
            "controls": False,
        },
        sink=_HEARTBEAT_DECISIONS,
    )
    return alloc


async def _beat(
    db: DbSession,
    ctx: CurrentPrincipal,
    alloc: ComputeAllocation | None,
    body: MachineHeartbeatRequest | None,
) -> Response:
    if alloc is None or alloc.state in ("released", "failed"):
        raise _not_found()
    said = body or MachineHeartbeatRequest()
    try:
        await machine_service.heartbeat(
            db,
            alloc,
            ctx=ctx,
            capacity=said.capacity,
            chats_served=said.chats_served,
            daemon_version=said.daemon_version,
            draining=said.draining,
            restarting=said.restarting,
            daemon_instance_id=said.daemon_instance_id,
            resources=said.resources.model_dump() if said.resources is not None else None,
            sandbox=said.sandbox,
            capabilities=said.capabilities,
            last_activity_at=said.last_activity_at,
            isolation=said.isolation.model_dump() if said.isolation is not None else None,
            fault=said.fault.model_dump() if said.fault is not None else None,
        )
    except (machine_service.StaleInstanceError, machine_service.BoxTooOldError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    # A box that backs an org machine is told what it is on every beat, so a
    # rename or a new idle setting reaches its chats; every other box gets the
    # bodiless answer it always has.
    card = await machine_service.box_card_for(db, alloc, org_id=ctx.org_id)
    if card is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=MachineHeartbeatResponse(card=card).model_dump(mode="json"),
    )


@router.get("/current", response_model=MachineStateRead)
async def current_machine(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    workspace_id: UUID | None = Query(default=None),
) -> MachineStateRead:
    """The machine the caller's next chat would run on. With ``workspace_id``,
    a chat of that workspace: a workspace pinned to an org machine places its
    chats there, not where the org's other chats go. A workspace the caller
    may not read is the same not-found as a missing one."""
    await _decide(request, db, ctx, user, action=Action.READ, resource_id="current", is_owner=True)
    pin = None
    if workspace_id is not None:
        workspace = await workspaces.load(db, workspace_id)
        if workspace is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        reader = await sharing.resolve_reader(
            db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
        )
        await enforce(
            request,
            db,
            ctx,
            Action.READ,
            sharing.object_resource(workspace, type=ResourceType.WORKSPACE),
            await sharing.workspace_attrs(db, workspace, reader),
        )
        pin = await compute.workspace_pin(db, workspace.id, org_id=ctx.org_id)
    return await machine_service.machine_state_read(
        db, ctx=ctx, org_id=ctx.org_id, workspace_pin=pin, owner_user_id=user.id
    )


@router.post(
    "/me/worker-credentials",
    response_model=MachineWorkerCredentialRead,
    status_code=status.HTTP_201_CREATED,
)
@machine_own_standing
async def mint_worker_credential(
    request: Request,
    body: MachineWorkerCredentialRequest,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> MachineWorkerCredentialRead:
    """A box mints the credential its process for ``body.org_id`` runs on.

    Only the machine credential itself may mint one, and only for an org it
    holds work in, by the rule the worker door re-asks on every request: a
    live chat bound to the machine, a workspace it still holds (a notebook
    kernel's sandbox with no chat on the box), or a live Files lease it is
    finishing on (the hand-back of a folder a chat moved off it). A pool box may
    be placed in any org, but a box that holds nothing for an org has no
    process to run for it, and a stolen credential must not mint its way in.
    The credential minted reaches that org alone; the decision is on record in
    that org's audit lane.
    """
    reached = await org_held_by(ctx, body.org_id)
    machine_id = await decide_machine_standing(
        request,
        db,
        ctx,
        action=Action.WRITE,
        resource_id=f"worker:{body.org_id}",
        org_id=body.org_id,
        org_reached=reached,
    )
    token, claims = mint_machine_worker_token(
        credential_id=UUID(ctx.credential_id or ""), machine_id=machine_id, org_id=body.org_id
    )
    return MachineWorkerCredentialRead(
        token=token,
        org_id=claims.org_id,
        expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
        expires_in=claims.expires_at - claims.issued_at,
    )


@router.get("/me/routing", response_model=MachineRoutingRead)
@machine_own_standing
async def machine_routing(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = None,
) -> MachineRoutingRead:
    """The chats bound to the box's machine, ids and states only. Read on the
    machine credential, never on a worker credential (whose process must not
    learn which other orgs share its box)."""
    await decide_machine_standing(
        request,
        db,
        ctx,
        action=Action.READ,
        resource_id="routing",
        org_id=ctx.org_id,
        org_reached=True,
    )
    return await routing_page(db, ctx, limit=limit, cursor=cursor)
