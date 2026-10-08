"""The realtime socket gateway and the HTTP routes around it.

``POST /api/v1/ws/tickets`` mints the one credential a socket accepts: a
30-second, single-use ticket bound to the caller's session. Rate-limited per
caller like every burst-sensitive route. ``GET /api/v1/ws/protocol`` is the
vocabulary this server speaks, so a client can pin itself against it.

``WS /api/v1/ws`` is the socket. It authenticates by hand — no dependency
runs on a WebSocket scope, and a router-level cookie dependency would answer
the handshake with a bare 403 and hide the reason — so the ticket router and
the socket router are mounted separately: the former behind the product gate,
the latter open, with the gate's checks (an active, unblocked account) re-run
inside the handshake and on every keepalive tick. A process that could not
deliver another replica's frames is refused first, before the ticket is read,
the same way the event stream refuses one.
"""

from __future__ import annotations

from typing import get_args

from alkera_core.machine_refusals import MACHINE_CREDENTIAL_REFUSED
from alkera_core.schemas.realtime import (
    CHANNEL_PATTERN,
    WS_PATH,
    WS_SUBPROTOCOL,
    WS_TICKET_SUBPROTOCOL_PREFIX,
    DocEnvelope,
    DocType,
    EnvelopeKind,
    OpIntent,
    RealtimeProtocolDescriptor,
    WsTicketResponse,
)
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status

from backend.api.rate_limit import charge_stream_open, limited
from backend.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    PrincipalUser,
    optional_session_claims,
)
from backend.services.realtime import close_codes, tickets
from backend.services.realtime import runtime as realtime_runtime
from backend.services.realtime import session as socket_session
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.limits import ConnectionGate, connection_key

ticket_router = APIRouter(prefix="/api/v1/ws", tags=["realtime"])
socket_router = APIRouter(prefix="/api/v1", tags=["realtime"])

_TICKET_THROTTLE = limited("ws_ticket")
_REGISTRY = DocRegistry()


@ticket_router.post(
    "/tickets",
    response_model=WsTicketResponse,
    dependencies=[Depends(_TICKET_THROTTLE)],
    summary="Mint a single-use socket ticket",
)
async def mint_ticket(
    request: Request, user: PrincipalUser, ctx: CurrentPrincipal, db: DbSession
) -> WsTicketResponse:
    """A ticket for the caller: a person's, bound to the session this request
    authenticated with, or — for a box on its own machine credential — a
    machine ticket bound to that credential. Every other credential is refused
    where ``CurrentUser`` refuses it."""
    if user is None:
        # A box on its credential. There is no session to bind to; the ticket
        # is bound to the credential, re-read here through the one standing
        # rule every door shares, and refused as the credential is refused.
        try:
            return await tickets.mint_for_machine(db, ctx)
        except tickets.MachineCannotMintError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": MACHINE_CREDENTIAL_REFUSED,
                    "message": "Machine credential refused",
                },
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
    claims = optional_session_claims(request)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session could not be re-read for the ticket",
            headers={"WWW-Authenticate": "Cookie"},
        )
    try:
        # A daemon mints with the agent headers on the request: the socket it
        # opens speaks as that agent (its machine), and the ticket carries it
        # so the handshake knows without re-reading any header.
        return tickets.mint(
            user,
            claims,
            org_id=ctx.org_id,
            agent_id=ctx.acting_principal.id if ctx.is_agent else None,
        )
    except ValueError as exc:
        # A session without a jti cannot be re-checked by the socket.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "session_cannot_open_sockets", "message": str(exc)},
        ) from exc


@ticket_router.get(
    "/protocol",
    response_model=RealtimeProtocolDescriptor,
    summary="The realtime protocol this server speaks",
)
async def protocol(user: CurrentUser) -> RealtimeProtocolDescriptor:
    return RealtimeProtocolDescriptor(
        schema_version=DocEnvelope.SCHEMA_VERSION,
        envelope_kinds=list(get_args(EnvelopeKind)),
        doc_types=list(get_args(DocType)),
        op_intents=list(get_args(OpIntent)),
        close_codes=dict(close_codes.CLOSE_CODES),
        path=WS_PATH,
        subprotocol=WS_SUBPROTOCOL,
        ticket_subprotocol_prefix=WS_TICKET_SUBPROTOCOL_PREFIX,
        channel_pattern=CHANNEL_PATTERN,
    )


@socket_router.websocket("/ws", dependencies=[Depends(limited("stream_open"))])
async def realtime_socket(websocket: WebSocket) -> None:
    runtime = realtime_runtime.runtime_of(websocket.app)
    if runtime is not None and runtime.draining:
        # This process is stopping and has closed its sockets; one admitted
        # now would be held by a hub about to go quiet. Refused before the
        # ticket is read, with the code the client reconnects on at once.
        await socket_session.refuse(
            websocket,
            socket_session.Refusal(close_codes.SERVICE_RESTART, "server restarting"),
        )
        return
    if runtime is None or not await realtime_runtime.can_deliver(runtime):
        # Everything from another replica — every doc.op, presence change and
        # announcement — reaches this socket through the outbox listener, so a
        # process without a connected one would serve a socket that sees only
        # its own writes. Refused BEFORE the ticket is read: a single-use
        # credential must not be spent on a replica that cannot deliver.
        await socket_session.refuse(
            websocket,
            socket_session.Refusal(close_codes.UNAVAILABLE, "realtime is not running here"),
        )
        return
    decision = await socket_session.admit(websocket)
    if isinstance(decision, socket_session.Refusal):
        await socket_session.refuse(websocket, decision)
        return
    gate = ConnectionGate.ws()
    if isinstance(decision, socket_session.MachineAdmission):
        # A box is counted under its machine, on the same key its event
        # stream is counted under: it is nobody's tab and takes nobody's slot.
        gate_key = f"machine:{decision.machine_id}"
        charge_key = gate_key
    else:
        gate_key = await connection_key(
            decision.user.id,
            websocket.headers,
            org_id=decision.claims.org_team_id,
            credential_id=decision.claims.jti,
        )
        charge_key = f"user:{gate_key}"
    # The handshake is the one moment a socket is counted: one ``stream_open``
    # against the admitted user, charged here rather than by the app-level
    # dependency (which cannot know the user before the ticket is read). The
    # frames that follow never touch a bucket — the session has its own
    # per-socket frame meter for a flood on one connection.
    if await charge_stream_open(websocket, key=charge_key) is not None:
        await socket_session.refuse(
            websocket, socket_session.Refusal(close_codes.TOO_MANY, "reconnecting too quickly")
        )
        return
    if not gate.try_acquire(gate_key):
        await socket_session.refuse(
            websocket, socket_session.Refusal(close_codes.TOO_MANY, "too many connections")
        )
        return
    # The route owns the capacity slot: taken above, freed exactly once here,
    # however the socket ends.
    try:
        await socket_session.run_socket(
            websocket, admission=decision, runtime=runtime, registry=_REGISTRY
        )
    finally:
        gate.release(gate_key)


__all__ = ["mint_ticket", "protocol", "realtime_socket", "socket_router", "ticket_router"]
