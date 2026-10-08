"""Where a reader left the workspace beside a chat.

A chat page is a chat next to a pane of tabs onto the chat's own folder, and a
reader who closes it expects to find it as they left it — on the next reload,
and on their other machine. That layout is therefore a row rather than browser
storage, and it is per reader: two people in one shared chat each keep their
own tabs, so the key is ``(chat_id, user_id)`` and neither can see or disturb
the other's.

The document in ``state`` is ids only (``ChatWorkspaceState``): a tab names a
node by id and never carries a URL, a token or bytes. The row is replayed by a
browser, so anything fetchable stored here would be a stored redirect; the
route refuses a document that is not ids-only and refuses an oversized one, so
this table can never grow into a content store.

Both foreign keys cascade. A deleted chat's layouts are meaningless, and a
deleted account's layouts are the account's own data — neither is worth
keeping as an orphan, and neither is on the audit trail (the decision to open
a chat is, in the outbox).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class ChatWorkspaceState(Base):
    __tablename__ = "chat_workspace_states"
    __table_args__ = (Index("ix_chat_workspace_states_user", "user_id"),)

    chat_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspace_objects.id", ondelete="CASCADE", name="fk_chat_workspace_states_chat"
        ),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_chat_workspace_states_user"),
        primary_key=True,
    )
    #: The org the chat belongs to, carried on the row so a tenancy sweep and a
    #: per-org purge never have to join back through the chat.
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: A dumped :class:`~alkera_core.schemas.objects.ChatWorkspaceState`.
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
