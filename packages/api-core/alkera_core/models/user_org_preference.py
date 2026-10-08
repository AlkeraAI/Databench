"""The preferences a person keeps per org: the server's copy.

Most of a person's preferences describe the person (their theme, their
permission stance, their notification taste) and live once per identity in
``user_preferences``. A few name something in ONE org's model catalog: the
effort chosen per model, the default chat model and its effort. A model that
exists in one org's catalog may not exist in another's, so those keys live here,
one document per (identity, org), and every reader and writer of them takes the
org from the request's credential (``alkera_core.schemas.preferences.ORG_SCOPED_KEYS``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, PrimaryKeyConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class UserOrgPreference(Base):
    __tablename__ = "user_org_preferences"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "org_team_id", name="pk_user_org_preferences"),
        Index("ix_user_org_preferences_org_team_id", "org_team_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE", name="fk_user_org_preferences_user")
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE", name="fk_user_org_preferences_org")
    )
    # Only the org-scoped keys of a ``Preferences`` document, as it serializes them.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
