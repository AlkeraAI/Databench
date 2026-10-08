"""A personal access token: a long-lived bearer credential that stands in for
one user.

Unlike a CI or proxy token (org-scoped, no human behind them) a personal access
token belongs to a USER and acts with that user's roles; the authorization layer
records every call it makes as ``[user, pat]``. The raw secret (``alk_pat_…``)
is shown once at mint and only its HMAC digest under the shared lookup-token
pepper is stored, exactly like the other bearer tokens, so a database read never
yields a replayable credential.

``org_team_id`` is denormalised from the owner so a token can be resolved and
tenancy-checked without joining ``users``, and so a user who somehow changed
org leaves a token that no longer matches (the resolver refuses it). ``scopes``
is recorded for a future narrowing of what a token may do; nothing enforces it
yet. There is no route that mints one in this version: the resolver, hashing,
expiry and revocation are what this table proves.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class PersonalAccessToken(Base):
    __tablename__ = "personal_access_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_personal_access_tokens_token_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # HMAC-SHA256(pepper, raw_secret) — the raw secret is shown once and never stored.
    # The membership the token acts through and its credential epoch at mint.
    # NULL on rows minted before tokens were membership-bound: those resolve by
    # (owner, org) and count as epoch 0.
    membership_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "org_memberships.id",
            ondelete="CASCADE",
            name="fk_personal_access_tokens_membership",
        ),
        nullable=True,
    )
    membership_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Optional auto-expiry (None = never).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when revoked; a revoked or expired token authenticates nothing.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
