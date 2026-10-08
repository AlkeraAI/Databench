"""Platform bans: a person, or every address at a domain, shut out of the product.

Both tables are append-only history. A ban is *active* while ``lifted_at`` is
NULL and *lifted* once it is stamped; lifting never deletes the row, so the
register of who was banned, by whom, for what, and who let them back in stays
readable. The partial unique indexes are the invariant "at most one active ban
per user / per domain" — declared here, not only in the migration, so
``alembic check`` sees them as part of the model.

A domain ban matches the user's stored ``email_domain`` exactly (lower-cased,
no leading ``@``): ``acme.com`` covers ``Alice@ACME.com`` and does NOT cover
``sub.acme.com`` — a subdomain is a different domain and takes its own ban.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class UserBan(Base):
    __tablename__ = "user_bans"
    __table_args__ = (
        Index(
            "uq_user_bans_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Free text for the register; may be empty. Never shown to the banned person.
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The platform admin who banned. SET NULL: the record outlives the admin.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def active(self) -> bool:
        return self.lifted_at is None


class EmailDomainBan(Base):
    __tablename__ = "email_domain_bans"
    __table_args__ = (
        Index(
            "uq_email_domain_bans_active_domain",
            "domain",
            unique=True,
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Normalized: lower-case, bare hostname, no leading "@" (see alkera_core.bans).
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def active(self) -> bool:
        return self.lifted_at is None
