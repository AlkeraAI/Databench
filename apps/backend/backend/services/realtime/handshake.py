"""The socket handshake's pieces that need no socket: where the browser says it
came from, which ticket the client offered, the session a person's ticket was
minted from, and what an admission or a refusal carries."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from alkera_core.auth import (
    SessionClaims,
    WsMachineTicket,
    WsTicket,
)
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.models import User
from alkera_core.schemas.realtime import (
    WS_TICKET_SUBPROTOCOL_PREFIX,
)


def _origin_key(value: str) -> str | None:
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def allowed_origins() -> frozenset[str]:
    """The browser origins a socket may come from: the CORS list and the portal's URL."""
    keys = {_origin_key(origin) for origin in settings.cors_origins_list}
    keys.add(_origin_key(settings.frontend_base_url))
    return frozenset(key for key in keys if key is not None)


def origin_allowed(origin: str | None) -> bool:
    """A missing ``Origin`` is a non-browser client and is allowed; a present one
    must match the allowed set exactly (``null`` and anything else is refused)."""
    if origin is None:
        return True
    key = _origin_key(origin)
    return key is not None and key in allowed_origins()


def ticket_from_subprotocols(offered: list[str]) -> str | None:
    """The ticket the client offered as ``alkera-ticket.<jwt>``; ``None`` when
    it offered none or more than one (two tickets is not a client we know)."""
    found = [
        entry.removeprefix(WS_TICKET_SUBPROTOCOL_PREFIX)
        for entry in offered
        if entry.startswith(WS_TICKET_SUBPROTOCOL_PREFIX)
    ]
    if len(found) != 1 or not found[0]:
        return None
    return found[0]


@dataclass(frozen=True, slots=True)
class Admission:
    user: User
    claims: SessionClaims
    ticket: WsTicket

    @property
    def agent_id(self) -> str | None:
        """The agent this socket speaks as — the ticket's assertion, minted
        from the agent headers the daemon sent — or ``None`` for a person."""
        return self.ticket.agent_id


@dataclass(frozen=True, slots=True)
class MachineAdmission:
    """A box admitted on its own machine credential: the ticket it offered and
    the context its credential resolved to at the handshake — the machine it
    holds, its operator org and the orgs it serves."""

    ticket: WsMachineTicket
    ctx: ActingContext

    @property
    def machine_id(self) -> str:
        return self.ctx.acting_principal.id


@dataclass(frozen=True, slots=True)
class Refusal:
    code: int
    reason: str


def session_claims_for(user: User, ticket: WsTicket) -> SessionClaims:
    """The session the ticket was minted from, as the revocation and membership
    checks read it. Ids (the org and membership among them) from the ticket;
    the rest from the live user row."""
    return SessionClaims(
        user_id=user.id,
        email=user.email,
        org_team_id=ticket.org_id,
        platform_role=user.platform_role,
        issued_at=ticket.session_issued_at,
        expires_at=ticket.session_expires_at,
        jti=ticket.session_jti,
        membership_id=ticket.membership_id,
        membership_epoch=ticket.membership_epoch,
    )
