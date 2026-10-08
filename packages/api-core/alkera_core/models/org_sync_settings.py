"""Per-org KB sync settings — org-scoped, admin-editable.

Safe day-one DEFAULTS make sync safe by construction. A NEW org is created
with sync on (``org_sync_settings_service.for_new_org``); an org with no row
reads as off, since it predates the default and never opted in. Once on, a
write-back still lands ``private``/unverified (never auto-broadcast); ``team``
promotion by membership, ``org`` by admin; never silent org-promote; standard
secret/PII strictness. A missing row means "all defaults", so existing orgs need
no backfill.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class OrgSyncSettings(Base):
    __tablename__ = "org_sync_settings"

    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    #: Master switch for central knowledge sync (new orgs start ON; no row reads OFF).
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    #: What an agent write-back is scoped to by default — ``private`` (fail-safe:
    #: it does NOT auto-broadcast).
    default_promotion: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="private"
    )
    #: The secret/PII gate strictness.
    classification_strictness: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="standard"
    )
    #: What may auto-promote vs require human/admin review.
    auto_promote_policy: Mapped[str] = mapped_column(
        String(48), nullable=False, server_default="human_or_self_verify"
    )
    #: The daemon ``kb_team_sync`` pull cadence.
    pull_cadence_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
