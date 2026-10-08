"""Federated identity link — one row per (provider, account) bound to a user.

This is the extensibility spine for external login. Google/GitHub are the first
two providers; enterprise OIDC SSO (Okta/Azure/Workspace) and SAML are future
adapters that write the same shape. `subject` is a free-form string (not a UUID)
precisely so OIDC `sub` claims and SAML NameIDs of any form fit unchanged.

Plain `Base` model: an identity link is current-state, not history. The audit
trail is `created_at` + `raw_profile` (last-seen claims).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base

if TYPE_CHECKING:
    from alkera_core.models.user import User


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Provider registry key: "google" | "github" | future "oidc-<org>" | "saml-<org>".
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    # Provider-stable account id (OIDC `sub`, GitHub numeric id as str, SAML NameID).
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    # The email the provider asserted at (re)link time — audit only; `users.email`
    # remains the canonical address.
    email_at_link: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # Last-seen normalized claims / userinfo — kept for audit + future fields.
    raw_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="oauth_identities")

    __table_args__ = (
        # The hard identity key: login resolves a user by (provider, subject).
        UniqueConstraint("provider", "subject", name="uq_oauth_identity_provider_subject"),
        # One link per provider per user (no two Google accounts on one user).
        UniqueConstraint("provider", "user_id", name="uq_oauth_identity_provider_user"),
    )
