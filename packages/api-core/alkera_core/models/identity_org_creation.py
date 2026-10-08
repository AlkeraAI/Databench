"""How many orgs a person has created: one row per org an identity created.

A single identity must not farm orgs, so the creation of orgs is capped per
person over a rolling window (``backend.services.abuse.org_creation``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class IdentityOrgCreation(Base):
    """One org created by an identity, kept for the per-identity creation cap. The
    row outlives the org (``SET NULL``) so deleting an org does not refund the
    creation."""

    __tablename__ = "identity_org_creations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_identity_org_creations_user_created", "user_id", "created_at"),)
