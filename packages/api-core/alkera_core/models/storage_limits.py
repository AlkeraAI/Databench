"""Storage ceilings set by hand: an org's override, and a member's own limit.

``org_storage_limits`` holds at most one row per org — the platform admin's
override of the plan figure. A row with ``limit_bytes`` NULL is an *explicit*
"unlimited"; no row at all means "the plan decides". The two are different
answers, which is why the override is a row and not a nullable column.

``user_storage_limits`` mirrors the money model: an org admin caps a member
org-wide (``team_id`` NULL) and a team admin caps them inside that team's
folder. One row per (org, team-or-null, user), by a named unique index.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class OrgStorageLimit(Base):
    __tablename__ = "org_storage_limits"

    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    # NULL = unlimited, on purpose; the row's existence is the override.
    limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserStorageLimit(Base):
    __tablename__ = "user_storage_limits"
    __table_args__ = (
        Index(
            "uq_user_storage_limits_org_user_orgwide",
            "org_team_id",
            "user_id",
            unique=True,
            postgresql_where=text("team_id IS NULL"),
        ),
        Index(
            "uq_user_storage_limits_org_team_user",
            "org_team_id",
            "team_id",
            "user_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = the org-wide limit; set = the limit inside that team's folder.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    limit_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
