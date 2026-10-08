"""Org-scoped proxy token — the credential a self-hosted gateway in *proxy*
mode authenticates to Alkera's hosted gateway with.

Unlike `AuthToken` (a per-USER JWT registry), a proxy token belongs to an ORG and
carries no user identity: Alkera meters the AGGREGATE usage under the token to the
org and postpaid-invoices it, so a customer's per-user identity never leaves their
VPC. The raw secret is shown ONCE at mint and only its HMAC hash is stored (looked
up by hash, like the single-use email tokens). A pure connection credential: the
org's commercial figures (per-seat allotment, per-period spend cap) live on its
``EnterprisePlan``, never per token.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class ProxyToken(Base):
    __tablename__ = "proxy_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # HMAC-SHA256(pepper, raw_secret) — the raw secret is shown once at mint and
    # never stored. Unique + indexed so the gateway resolves a presented token in
    # one indexed lookup.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional auto-expiry (None = never).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when revoked; a revoked or expired token authenticates nothing.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
