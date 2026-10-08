"""OrgMembership: the tenancy binding of one identity to one org.

A ``users`` row is an identity: it authenticates (password, OAuth, SSO, MFA) and
owns nothing tenant-shaped. This table is what puts an identity inside an org:
one row per (identity, org), with its own lifecycle, its own credential epoch,
the org's SSO break-glass flag and the org's SCIM id for the person. Org admins
and the org's SCIM act on this row and never on the identity, because the
identity may belong to other orgs.

Every credential that reaches tenant data names exactly one membership (the
``mid`` and ``mep`` claims, or the ``membership_id`` column of a row
credential). Bumping ``credential_epoch`` retires every credential the person
holds in THIS org and nowhere else; ``users.token_epoch`` stays the lever for
identity events (password change, ban, "sign out everywhere").

``org_team_id`` is always a root team (an org): the service that writes rows
checks it, and the backfill and the ``users`` triggers only ever write a
user's home org.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models._enums import MembershipStatus


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "org_team_id", name="uq_org_memberships_user_org"),
        Index("ix_org_memberships_org_status", "org_team_id", "status"),
        Index("ix_org_memberships_user", "user_id"),
        # An org's SCIM id names one person in that org; the same IdP id may
        # appear in another org for somebody else.
        Index(
            "uq_org_memberships_org_scim",
            "org_team_id",
            "scim_external_id",
            unique=True,
            postgresql_where=text("scim_external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_org_memberships_user"),
        nullable=False,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_memberships_org"),
        nullable=False,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(
            MembershipStatus,
            native_enum=False,
            length=16,
            name="membership_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=MembershipStatus.ACTIVE,
        server_default=MembershipStatus.ACTIVE.value,
    )
    # The per-org twin of users.token_epoch. Every credential minted for this
    # membership carries the value it was minted at; a bump refuses all of them.
    credential_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sso_exempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The ``name.givenName`` / ``name.familyName`` this org's IdP pushed for the
    #: person. ``None`` when it never sent one.
    scim_given_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scim_family_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.status is MembershipStatus.ACTIVE
