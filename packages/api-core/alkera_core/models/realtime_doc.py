"""A synchronised document: the server-side state one ``doc:<type>:<id>``
channel converges on.

One row per (org, document). The org is part of the key on purpose: a chat
document's id is chosen by the client, so two tenants can name the same one,
and a key of ``(doc_type, doc_id)`` alone would hand the second tenant the
first tenant's row to read — or refuse it a document it is entitled to. Each
org therefore holds its own document under an id, and no look-up on this
table is meaningful without naming the org it is for.

``epoch`` and ``seq`` are the server's: a client's operation is accepted only
against the current epoch, the sequence it was applied at is stamped before it
is rebroadcast, and a rebuild (a publisher snapshot, an artifact edited behind
the document's back) bumps the epoch and resets the sequence so every peer
re-fetches. ``state`` is the whole document as a JSON object — capped by the
application, uncompacted in this version.

``owner_user_id`` is who may write (for a chat, the publishing peer; for an
artifact, the knowledge item's owner decides instead and this column is
informational). ``team_id`` narrows who may READ a chat document to members of
that team; null means every member of the org. Rows cascade with their org; a
deleted owner or team leaves the document readable and unwritable rather than
gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class RealtimeDoc(Base):
    __tablename__ = "realtime_docs"
    __table_args__ = (
        CheckConstraint("epoch >= 1", name="ck_realtime_docs_epoch_positive"),
        CheckConstraint("seq >= 0", name="ck_realtime_docs_seq_nonnegative"),
        CheckConstraint("doc_type IN ('chat', 'artifact')", name="ck_realtime_docs_doc_type"),
        Index("ix_realtime_docs_org_id", "org_id"),
        Index("ix_realtime_docs_owner_user_id", "owner_user_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_realtime_docs_org_id_teams"),
        primary_key=True,
    )
    doc_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="SET NULL", name="fk_realtime_docs_team_id_teams"),
        nullable=True,
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_realtime_docs_owner_user_id_users"),
        nullable=True,
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: The turn's liveness, kept OUT of ``state``. A chat that is working says
    #: so every fifteen seconds for as long as the turn runs, and the only
    #: thing that changes between two of those is the instant. Carried in the
    #: JSON blob, each restamp re-serialised the whole document — a transcript
    #: window and every meta key — wrote it back and ran the compaction pass,
    #: four times a minute per running chat, to move one timestamp. Here it is
    #: two small columns updated in place; a reader gets them folded back over
    #: ``state.meta`` so the wire shape is unchanged. ``NULL`` until a turn has
    #: stamped itself, which is what every document written before this was.
    turn_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    turn_state_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
