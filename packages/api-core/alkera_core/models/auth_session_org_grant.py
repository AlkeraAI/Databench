"""AuthSessionOrgGrant: how a browser login session authenticated, and when.

A login session (a refresh family) is identity-level: it can mint an access
token for any active membership of its identity, subject to that org's sign-in
policy. The policy asks two questions of the family: which methods did it
authenticate with, and how long ago. Each row answers one of them.

``org_team_id`` NULL is an identity-level grant: a password, Google or GitHub
sign-in, valid toward any org whose policy allows that method. A non-NULL org
is a grant FOR that org only (an SSO assertion from that org's IdP).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class AuthSessionOrgGrant(Base):
    __tablename__ = "auth_session_org_grants"
    __table_args__ = (
        # NULLS NOT DISTINCT: an identity-level grant (org NULL) is recorded
        # once per method per family, like an org grant.
        UniqueConstraint(
            "family_id",
            "org_team_id",
            "method",
            name="uq_auth_session_org_grants",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_auth_session_org_grants_family", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The refresh family (``auth_refresh_tokens.family_id``). Not a foreign key:
    # a family is a set of rows, not one row.
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_auth_session_org_grants_org"),
        nullable=True,
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
