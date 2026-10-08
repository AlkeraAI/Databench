"""Invitation model.

A pending offer from an Org/Team Admin to a specific email address to join
a specific team. The token is a random URL-safe string the recipient
presents to /api/v1/auth/signup or /api/v1/invitations/{token}/accept.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base
from alkera_core.db.errors import register_unique
from alkera_core.models._enums import InvitationStatus, TeamRole

if TYPE_CHECKING:
    from alkera_core.models.team import Team
    from alkera_core.models.user import User


#: At most one pending invitation for an (email, team) pair.
PENDING_EMAIL_TEAM_INDEX = "uq_invitations_pending_email_team"
#: What a second pending invitation for the pair is told.
PENDING_EXISTS_MESSAGE = "A pending invitation for this email and team already exists"


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        # Partial unique index: at most one PENDING invitation for a given
        # (email, team_id) pair. Re-inviting after accept/reject is allowed.
        # Declared here so Alembic autogenerate sees it (created in migration
        # 0003) and does not propose dropping it.
        Index(
            PENDING_EMAIL_TEAM_INDEX,
            "email",
            "team_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[TeamRole] = mapped_column(
        Enum(
            TeamRole,
            native_enum=False,
            length=32,
            name="team_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TeamRole.MEMBER,
    )
    token: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(
            InvitationStatus,
            native_enum=False,
            length=32,
            name="invitation_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=InvitationStatus.PENDING,
    )
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    team: Mapped[Team] = relationship()
    invited_by: Mapped[User | None] = relationship()


register_unique(PENDING_EMAIL_TEAM_INDEX, "email", PENDING_EXISTS_MESSAGE)
