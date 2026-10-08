"""An email domain assigned to one org's SSO connection.

An email domain is a name every org shares. The domains an org's identity
provider speaks for are rows here, and a domain belongs to at most one org: the
unique constraint on ``domain`` holds that whatever writes the table. Only
platform staff assign or remove them (``/admin/v1/orgs/{org}/sso/domains``);
an org admin cannot.

Every decision that reads an org's domains goes through
``alkera_core.auth.sso_domains``: sign-in discovery, what the org's IdP may
assert, SCIM provisioning and SSO enforcement.

Proof of ownership is not modelled. Staff assign a domain after checking it
themselves. A DNS (or other) proof can be added later as a state on this row,
with only proven rows counting, without changing any reader: they all ask the
owner module.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
from alkera_core.db.errors import register_unique

DOMAIN_KEY = "uq_sso_domain_claims_domain"
DOMAIN_HELD_CODE = "sso_domain_held"
#: What the loser of a race for a domain is told. The check before the write
#: names the domain; the constraint cannot say which of several it refused.
DOMAIN_HELD_MESSAGE = "One of these domains is already assigned to another organization."


class SsoDomainClaim(Base):
    __tablename__ = "sso_domain_claims"
    __table_args__ = (
        UniqueConstraint("domain", name=DOMAIN_KEY),
        CheckConstraint("domain = lower(domain)", name="ck_sso_domain_claims_domain_lower"),
        Index("ix_sso_domain_claims_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_sso_domain_claims_org_team_id_teams"),
        nullable=False,
    )
    # Lowercase, no trailing dot.
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    # The staff member who assigned it; NULL for a domain carried over from the
    # connection's own list when assignment moved to staff.
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="SET NULL", name="fk_sso_domain_claims_assigned_by_id_users"
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


register_unique(DOMAIN_KEY, "domains", DOMAIN_HELD_MESSAGE, code=DOMAIN_HELD_CODE)
