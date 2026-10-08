"""TeamMembership join model.

Links a user to a team with a role (`MEMBER` or `ADMIN`). One row per
(user, team) pair. ON DELETE CASCADE on both sides — removing a user or a
team should remove its memberships, not orphan them.

``org_team_id`` is the root of ``team_id`` (the org the team lives in), filled
by a database trigger when a writer leaves it unset. The composite foreign key
onto ``org_memberships(user_id, org_team_id)`` makes a team membership without
an org membership unrepresentable, and deleting the org membership removes
every team membership the person held in that org. It is deferred to commit so
a writer may add the org membership and the team rows in either order inside
one transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base
from alkera_core.models._enums import TeamRole

if TYPE_CHECKING:
    from alkera_core.models.team import Team
    from alkera_core.models.user import User


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The org (root team) ``team_id`` belongs to. No Python default: the
    # ``trg_team_memberships_org`` trigger fills it from the team's root (and
    # refuses a value that is not that root), and the insert reads it back.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, server_default=FetchedValue()
    )
    role: Mapped[TeamRole] = mapped_column(
        Enum(
            TeamRole,
            native_enum=False,
            length=32,
            name="team_role",
            # Store the StrEnum's `value` ('member'/'admin'), not the Python
            # member name ('MEMBER'/'ADMIN'). The DB CHECK constraint expects
            # the lowercase values.
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TeamRole.MEMBER,
        server_default=TeamRole.MEMBER.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "team_id", name="uq_team_memberships_user_team"),
        ForeignKeyConstraint(
            ["user_id", "org_team_id"],
            ["org_memberships.user_id", "org_memberships.org_team_id"],
            name="fk_team_memberships_org_membership",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_team_memberships_user_org", "user_id", "org_team_id"),
        # Serves the row-level policy when it is the only selective predicate.
        Index("ix_team_memberships_org_team_id", "org_team_id"),
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    team: Mapped[Team] = relationship(back_populates="memberships")
