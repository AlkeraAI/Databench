"""A Files node linked to a chat: one row per link.

A chat's attachments are unbounded over its life — the files it accumulates
are the conversation — so they are rows rather than a list inside the chat's
spec. A JSONB array is read whole, rewritten whole and locked whole: attaching
the ten-thousandth file re-serialised ten thousand ids under the chat row's
lock, and every read of the chat carried all of them to a browser that renders
a handful. A row costs the same to write at any length, and a page costs the
same to read.

The link is a REFERENCE and never a grant: every read of a linked node is
authorized again per request against the node itself, so a chat shared more
widely than a file does not widen the file. That is also why there is no
foreign key on ``node_id`` — the reference deliberately outlives what it points
at, so a node that was trashed or purged still detaches, and a list still shows
that something was attached here.

``position`` is the order the links were made in, which is the order a reader
sees them; it is assigned from the highest one the chat holds, under a lock
the chat's attaches take in turn, so each attach gets a place of its own.
Attaches made before that lock existed can still share a number, which is why
the order is ``(position, node_id)`` and not ``position`` alone — and why the
cursor a page resumes on is that whole pair. It ends in the primary key, so it
is unique even where the place is not: ties break the same way on every read,
and a page that ends inside one resumes inside it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"
    __table_args__ = (
        Index("ix_chat_attachments_chat_id_position", "chat_id", "position", "node_id"),
    )

    #: The pair IS the identity: attaching the same node twice is a link that
    #: is already there, not a second attachment, so the primary key refuses
    #: the duplicate rather than the writer having to read first.
    chat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspace_objects.id",
            ondelete="CASCADE",
            name="fk_chat_attachments_chat_id_workspace_objects",
        ),
        primary_key=True,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
