"""A single sign-on assertion waiting for its identity's owner to confirm it.

An org's IdP asserted an email that belongs to an existing identity holding no
membership in that org. The IdP proves the address to its org; it is not the
identity owner's consent to add a new way into their account. So nothing is
linked at the callback: the assertion is parked here for a few minutes, keyed by
the digest of a random value held only in an HttpOnly cookie on the browser that
ran the sign-in, until that person signs in AS the existing identity with one of
its own methods and confirms. Single use (``consumed_at``), short-lived
(``expires_at``), and gone with its org.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class SsoLinkRequest(Base):
    __tablename__ = "sso_link_requests"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sso_link_requests_token_hash"),
        Index("ix_sso_link_requests_expires_at", "expires_at"),
        Index("ix_sso_link_requests_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # SHA-256 hex of the cookie value; the value itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_sso_link_requests_org"),
        nullable=False,
    )
    # The org's provider key (``sso_service.provider_key(org)``) and the IdP's
    # stable subject: the ``oauth_identities`` row a confirmation creates.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    raw_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
