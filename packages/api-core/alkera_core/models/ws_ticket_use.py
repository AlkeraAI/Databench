"""A consumed socket ticket ``jti`` — the single-use record behind the
realtime handshake.

A socket ticket is a 30-second JWT; without this table it could open as many
sockets as fit in 30 seconds, from as many places as it was copied to. A
handshake records the ticket's ``jti`` here in its own committed transaction
before it trusts the ticket; a second handshake with the same ticket — on
this replica or any other, since every replica shares the table — collides
on the primary key and is refused. Rows older than the ticket TTL plus a
margin serve no replay check any more (the ticket itself has expired) and are
deleted by the realtime sweeper, not by the handshake — a purge on the
handshake path would put a table scan in front of every socket. ``consumed_at``
is indexed so that sweep reads only the rows it deletes. Same shape and
discipline as the GitHub install-claim record.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class WsTicketUse(Base):
    __tablename__ = "ws_ticket_uses"
    __table_args__ = (Index("ix_ws_ticket_uses_consumed_at", "consumed_at"),)

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
