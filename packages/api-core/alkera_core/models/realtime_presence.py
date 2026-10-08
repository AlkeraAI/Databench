"""Who is on a document channel right now.

One row per (channel, peer). A peer joins, heartbeats every keepalive tick
and leaves; a row unheard from for longer than the presence TTL is gone — the
roster hides it and a sweeper deletes it — so a socket that died without
saying goodbye does not haunt the roster. Deliberately NOT keyed to
``realtime_docs``: a viewer may wait on a channel before its producer creates
the document. Rows are per org (no foreign key, like the outbox: nothing
should ever block a tenant delete on a presence row) and vanish with the user.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class RealtimePresence(Base):
    __tablename__ = "realtime_presence"
    __table_args__ = (
        Index("ix_realtime_presence_last_seen_at", "last_seen_at"),
        Index("ix_realtime_presence_user_id", "user_id"),
    )

    doc_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    peer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_realtime_presence_user_id_users"),
        nullable=False,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
