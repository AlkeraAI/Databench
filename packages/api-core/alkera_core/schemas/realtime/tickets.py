"""The HTTP shapes around the socket: the ticket a client mints before it
connects, and the protocol descriptor it can read to learn the vocabulary.

In-flight shapes on plain ``BaseModel``. Both are OpenAPI-exposed so the
generated clients carry the envelope kinds, doc types, intents and close
codes as types rather than as strings copied by hand.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.schemas.realtime.envelope import DocType, EnvelopeKind, OpIntent

#: Where the socket lives. Under ``/api/`` on purpose: a bare ``/ws`` is
#: swallowed by the SPA's nginx and the Vite dev proxy.
WS_PATH: Final = "/api/v1/ws"
#: The subprotocol a client offers first and the server selects.
WS_SUBPROTOCOL: Final = "alkera-v1"
#: A client also offers ``<prefix><ticket>``; the ticket therefore travels in
#: the ``Sec-WebSocket-Protocol`` header and never in a URL an access log
#: would keep. The server never selects this entry.
WS_TICKET_SUBPROTOCOL_PREFIX: Final = "alkera-ticket."


class WsTicketResponse(BaseModel):
    """``POST /api/v1/ws/tickets``: a single-use, short-lived credential for
    one socket handshake. A box a release behind parses it, so a field
    this build does not know is ignored, never refused."""

    ticket: str = Field(min_length=1)
    expires_in: int = Field(ge=1, description="Seconds until the ticket dies unused.")
    path: str = Field(default=WS_PATH, description="The socket path to connect to.")


class RealtimeProtocolDescriptor(BaseModel):
    """``GET /api/v1/ws/protocol``: the vocabulary this server speaks, so a
    client can pin itself against it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(description="The DocEnvelope schema version this server writes.")
    envelope_kinds: list[EnvelopeKind]
    doc_types: list[DocType]
    op_intents: list[OpIntent]
    close_codes: dict[str, int] = Field(description="Close-code names to their numeric codes.")
    path: str = WS_PATH
    subprotocol: str = WS_SUBPROTOCOL
    ticket_subprotocol_prefix: str = WS_TICKET_SUBPROTOCOL_PREFIX
    channel_pattern: str = Field(description="The regular expression a channel name must match.")


__all__ = [
    "WS_PATH",
    "WS_SUBPROTOCOL",
    "WS_TICKET_SUBPROTOCOL_PREFIX",
    "RealtimeProtocolDescriptor",
    "WsTicketResponse",
]
