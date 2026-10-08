"""One-time-use record for consumed SAML assertions (replay defense).

A signed SAML assertion is valid until its NotOnOrAfter; within that window the
exact bytes could otherwise be POSTed to the ACS more than once. We record each
consumed (org, assertion id) and refuse a second use. ``expires_at`` (the
assertion's NotOnOrAfter) lets a sweeper drop rows once they can no longer be
replayed anyway.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class SamlReplayAssertion(Base):
    __tablename__ = "saml_replay_assertions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The assertion's NotOnOrAfter — after this it can't be replayed, so it's a
    # GC horizon for the sweeper.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The replay guard itself: one consumption per (org, assertion id).
        UniqueConstraint("org_team_id", "assertion_id", name="uq_saml_replay_org_assertion"),
    )
