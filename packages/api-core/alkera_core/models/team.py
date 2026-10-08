"""Team model.

A `Team` is the spec's tenancy unit. The org-level Team is the one with
`is_root=True` and `parent_team_id IS NULL`; child teams form a tree under
that root via the self-referential `parent_team_id` foreign key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base

if TYPE_CHECKING:
    from alkera_core.models.team_membership import TeamMembership


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_root: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=false(),
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    parent: Mapped[Team | None] = relationship(
        "Team",
        remote_side="Team.id",
        back_populates="children",
    )
    children: Mapped[list[Team]] = relationship(
        "Team",
        back_populates="parent",
        cascade="all, delete-orphan",
        single_parent=True,
    )
    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Roots are always at the top of their tree; non-roots may or may not
        # have a parent (a non-root with parent_team_id NULL is a malformed
        # standalone, but we don't currently forbid that — only the inverse).
        CheckConstraint(
            "(is_root = TRUE AND parent_team_id IS NULL) OR is_root = FALSE",
            name="ck_teams_root_has_no_parent",
        ),
    )
