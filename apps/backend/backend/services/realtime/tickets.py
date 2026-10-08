"""Socket tickets: minted from a session, burned once at the handshake.

A ticket is the one credential a socket accepts. It is minted over an
ordinary authenticated request (cookie or Bearer), lives thirty seconds,
carries ids only, and is bound to the session it came from so the socket can
re-check that session on a timer. ``burn`` is what makes it single-use across
the whole fleet: an insert-or-conflict in its own committed transaction, so a
replay on any replica collides on the primary key and is refused. It does that
one insert and nothing else — clearing out spent rows is
:func:`purge_consumed`, which the realtime sweeper runs on its own timer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from alkera_core.auth import SessionClaims, encode_ws_machine_ticket, encode_ws_ticket
from alkera_core.auth.machine_credential_standing import live_machine_of
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User, WsTicketUse
from alkera_core.schemas.realtime import WS_PATH, WsTicketResponse
from sqlalchemy import CursorResult, Interval, delete, func
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Consumed-ticket rows older than the TTL plus this margin serve no replay
#: check (the ticket itself has expired) and the sweeper deletes them.
PURGE_MARGIN = timedelta(days=1)


class MachineCannotMintError(Exception):
    """The machine credential behind the request stands behind no live machine
    — revoked, rotated away, never claimed, or its machine gone. One answer for
    all of them, so the refusal says nothing about which."""


def mint(
    user: User, claims: SessionClaims, *, org_id: UUID, agent_id: str | None = None
) -> WsTicketResponse:
    """A fresh ticket for ``user`` in ``org_id`` (the request's org) bound to
    ``claims`` (the session the request authenticated with, and the
    membership it names), speaking as ``agent_id`` when the mint request
    carried the agent assertion. Raises ``ValueError`` for a session that
    cannot mint one (no ``jti``, not this user's, or another org's)."""
    ttl = settings.realtime_ws_ticket_ttl_seconds
    ticket = encode_ws_ticket(
        user_id=user.id,
        org_id=org_id,
        session=claims,
        ttl_seconds=ttl,
        agent_id=agent_id,
    )
    return WsTicketResponse(ticket=ticket, expires_in=ttl, path=WS_PATH)


async def mint_for_machine(db: AsyncSession, ctx: ActingContext) -> WsTicketResponse:
    """A fresh ticket for a box on its own machine credential.

    The credential the request resolved is read again by id, through the one
    standing rule every door shares: a revoke that landed between the door and
    this mint ends here, and a credential that has claimed no machine mints
    nothing — there is no channel a socket for it could hold. Raises
    :class:`MachineCannotMintError`; a context that is not a machine's is a
    programming error, not a refusal.
    """
    if not ctx.is_machine or ctx.credential_id is None:
        raise ValueError("a machine ticket is minted for a machine principal")
    credential_id = UUID(ctx.credential_id)
    machine_id = await live_machine_of(db, credential_id)
    if machine_id is None or str(machine_id) != ctx.acting_principal.id:
        raise MachineCannotMintError("machine credential refused")
    ttl = settings.realtime_ws_ticket_ttl_seconds
    ticket = encode_ws_machine_ticket(
        credential_id=credential_id,
        machine_id=machine_id,
        org_id=ctx.org_id,
        ttl_seconds=ttl,
        org_bound=ctx.is_machine_worker,
    )
    return WsTicketResponse(ticket=ticket, expires_in=ttl, path=WS_PATH)


async def burn(
    jti: str, *, session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal
) -> bool:
    """Record ``jti`` consumed and report whether THIS call consumed it
    (``False`` = already spent).

    Its own session and its own commit on purpose: a burn that shared the
    handshake's transaction would be undone by any later refusal and leave the
    ticket alive to be replayed. One insert and nothing else: every handshake
    runs this, so it must not carry housekeeping for the whole table.
    """
    async with session_factory() as own:
        result = await own.execute(
            pg_insert(WsTicketUse).values(jti=jti).on_conflict_do_nothing(index_elements=["jti"])
        )
        burned = bool(cast("CursorResult[Any]", result).rowcount)
        await own.commit()
    return burned


async def purge_consumed(db: AsyncSession) -> int:
    """Delete consumed-ticket rows past the ticket TTL plus :data:`PURGE_MARGIN`
    and report how many; the caller commits.

    A row that old cannot refuse anything — the ticket it records expired long
    ago — so keeping it buys nothing. The cutoff is computed in SQL, never from
    the application clock, so a skewed process can never delete (and thereby
    un-burn) a row a live handshake just wrote. Runs on the realtime sweeper's
    timer, off the handshake path, against an index on ``consumed_at``.
    """
    purge_age = timedelta(seconds=settings.realtime_ws_ticket_ttl_seconds) + PURGE_MARGIN
    result = await db.execute(
        delete(WsTicketUse).where(
            WsTicketUse.consumed_at < func.now() - sa_cast(purge_age, Interval)
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "PURGE_MARGIN",
    "MachineCannotMintError",
    "burn",
    "mint",
    "mint_for_machine",
    "purge_consumed",
]
