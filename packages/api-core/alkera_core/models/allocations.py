"""A team's own allowance of budget or storage, set by an admin above it.

One row per (team, resource). ``resource`` is ``budget`` (``limit_nanos``,
nano-USD per ``window``) or ``storage`` (``limit_bytes``); the other limit
column stays NULL, which a CHECK pins. A member of the team with no limit of
their own may spend up to the team's whole allowance; a member of several
teams gets the sum (see ``alkera_core.allocation_tree``). Limits are
safeguards: the allowances of a team's members and sub-teams may add up to
more than the team's own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class AllocationResource(StrEnum):
    BUDGET = "budget"
    STORAGE = "storage"


class AllocationWindow(StrEnum):
    #: Resets with the org's billing cycle.
    CYCLE = "cycle"
    #: A running total that never resets.
    NONE = "none"


class TeamAllocation(Base):
    __tablename__ = "team_allocations"
    __table_args__ = (
        UniqueConstraint("team_id", "resource", name="uq_team_allocations_team_resource"),
        CheckConstraint("resource IN ('budget', 'storage')", name="ck_team_allocations_resource"),
        CheckConstraint("\"window\" IN ('cycle', 'none')", name="ck_team_allocations_window"),
        CheckConstraint(
            "(resource = 'budget' AND limit_bytes IS NULL)"
            " OR (resource = 'storage' AND limit_nanos IS NULL)",
            name="ck_team_allocations_limit_column",
        ),
        Index("ix_team_allocations_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    resource: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_nanos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    window: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=AllocationWindow.CYCLE.value
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
