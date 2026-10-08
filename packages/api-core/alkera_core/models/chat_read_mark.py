"""How far each person has read each chat.

Read state is per person: two people in one shared chat each keep their own
mark, so the key is ``(chat_id, user_id)`` and one reading never clears the
chat for the other. ``last_read_seq`` names the transcript sequence
(``chat_messages.seq``) of the last event the person has seen and only moves
forward; ``marked_unread`` is the person's own "Mark as unread", cleared by
their next read. A person with no row has read nothing.

Both foreign keys cascade: a deleted chat's marks mean nothing, and a deleted
account's marks are the account's own data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class ChatReadMark(Base):
    __tablename__ = "chat_read_marks"
    __table_args__ = (
        CheckConstraint("last_read_seq >= 0", name="ck_chat_read_marks_seq_nonnegative"),
        Index("ix_chat_read_marks_user", "user_id"),
    )

    chat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("workspace_objects.id", ondelete="CASCADE", name="fk_chat_read_marks_chat"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_chat_read_marks_user"),
        primary_key=True,
    )
    #: The chat's org, carried on the row so a tenancy sweep and a per-org
    #: purge never have to join back through the chat.
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    last_read_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    marked_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
