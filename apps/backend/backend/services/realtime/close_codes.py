"""WebSocket close codes the gateway sends, in the 4000-4999 range the
protocol reserves for applications.

Every refusal carries a code the client can act on: re-mint a ticket, stop,
or back off. The socket is accepted before it is closed with one of these
even when the decision was made before the handshake completed — a browser
cannot read the status of a refused handshake, only a bare 1006, and a client
that cannot tell "your ticket died" from "you are not allowed here" can only
retry forever.
"""

from __future__ import annotations

from typing import Final

#: No ticket, a bad / expired / already-used one, or a session that is no
#: longer good at the handshake. The client mints a new ticket once, then
#: backs off.
UNAUTHORIZED: Final = 4401
#: A browser ``Origin`` outside the allowed set. The client stops.
ORIGIN_FORBIDDEN: Final = 4403
#: Reserved for a channel the socket may not learn exists (in-band
#: ``not_found`` is the normal answer).
NOT_FOUND: Final = 4404
#: The session was revoked or expired, the account was blocked, or the socket
#: outlived its maximum life. The client mints a new ticket and reconnects.
SESSION_EXPIRED: Final = 4408
#: A frame over the size cap.
FRAME_TOO_LARGE: Final = 4413
#: The per-user or per-process connection cap, or the frame rate limit.
TOO_MANY: Final = 4429
#: The server is going away; reconnect.
SERVER_RESET: Final = 4500
#: This process cannot deliver: it runs without the outbox listener every
#: cross-replica frame arrives through, or that listener holds no connection.
#: A socket here would only ever see what this process itself wrote, so it is
#: refused at the handshake. The client re-mints and reconnects, landing on a
#: replica that can deliver (the event stream answers ``503`` for the same
#: reason).
UNAVAILABLE: Final = 4503

#: The WebSocket protocol's own "protocol error" (RFC 6455 §7.4.1), sent to a
#: verified machine's socket that speaks a ``machine.*`` frame this server does
#: not know or cannot parse. The box is the one peer that frame vocabulary
#: belongs to, and a box sending something malformed on it is a bug to surface
#: at once, not a frame to answer in-band and wait for more of. Kept out of
#: :data:`CLOSE_CODES`, which lists the application's own 4xxx codes.
PROTOCOL_ERROR: Final = 1002

#: The "service restart" code of RFC 6455 §7.4.1: this process is stopping. Sent to
#: every open socket when the process drains, and to a handshake that arrives
#: while it does; the client reconnects and lands on a replica that is staying.
#: Kept out of :data:`CLOSE_CODES`, like :data:`PROTOCOL_ERROR`: it is the protocol's own.
SERVICE_RESTART: Final = 1012

CLOSE_CODES: Final[dict[str, int]] = {
    "UNAUTHORIZED": UNAUTHORIZED,
    "ORIGIN_FORBIDDEN": ORIGIN_FORBIDDEN,
    "NOT_FOUND": NOT_FOUND,
    "SESSION_EXPIRED": SESSION_EXPIRED,
    "FRAME_TOO_LARGE": FRAME_TOO_LARGE,
    "TOO_MANY": TOO_MANY,
    "SERVER_RESET": SERVER_RESET,
    "UNAVAILABLE": UNAVAILABLE,
}

__all__ = [
    "CLOSE_CODES",
    "FRAME_TOO_LARGE",
    "NOT_FOUND",
    "ORIGIN_FORBIDDEN",
    "PROTOCOL_ERROR",
    "SERVER_RESET",
    "SERVICE_RESTART",
    "SESSION_EXPIRED",
    "TOO_MANY",
    "UNAUTHORIZED",
    "UNAVAILABLE",
]
