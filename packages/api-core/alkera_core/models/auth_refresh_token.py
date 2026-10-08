"""AuthRefreshToken model — the server-side half of a browser session.

A browser session is a FAMILY of refresh tokens: one row per token ever
minted, all sharing ``family_id``. Login starts a family; every refresh
rotates it (the presented row is stamped ``used_at`` and a child row is
minted); logout or any revocation stamps every row in the family. Only a
keyed hash of the token is stored — the raw value travels solely in the
refresh cookie.

A rotated row names its one child (``successor_id``) and holds the child's
raw value sealed under its own, so a concurrent re-presentation inside the
grace is handed the same child rather than a second one.

Each row also names the access token minted beside it (``access_jti``), so
revoking a family revokes its live access token too, and a long-lived
connection can ask "is the family behind this access token still alive?".

A family is identity-level. ``active_org_team_id`` names the org its access
tokens are minted for; NULL is the identity's home org (every family started
before the column existed reads that way). Rotation copies it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class AuthRefreshToken(Base):
    __tablename__ = "auth_refresh_tokens"
    __table_args__ = (
        Index("ix_auth_refresh_tokens_active_org", "active_org_team_id"),
        Index("ix_auth_refresh_tokens_successor", "successor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Shared by every rotation of one browser session; the id the sessions
    # list shows and a user revokes.
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # HMAC-SHA256 of the raw token under the token-hash pepper (64 hex chars).
    token_hash: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    # The `jti` of the access token minted with this row. Unique: one access
    # token per rotation.
    access_jti: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    # When the family began (login), carried onto every rotation so the
    # sessions list can say "signed in since".
    family_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The last request that presented THIS row (its mint, until it is used).
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Dies of neglect here — pushed forward by each rotation, never past the
    # absolute expiry.
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Fixed when the family began; no rotation moves it.
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Stamped when the row is rotated. A used row presented again (past the
    # short concurrent-refresh grace) is reuse: the family is revoked.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The one child this row was rotated into.
    successor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "auth_refresh_tokens.id", ondelete="SET NULL", name="fk_auth_refresh_tokens_successor"
        ),
        nullable=True,
    )
    # The child's raw value, sealed (AES-256-GCM) under a key derived from THIS
    # row's raw value, which is never stored: a concurrent re-presentation within
    # the grace opens it to receive the same child; a database read cannot.
    successor_sealed: Mapped[str | None] = mapped_column(String(160), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The org this family mints access tokens for; NULL is the home org.
    active_org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_auth_refresh_tokens_active_org"),
        nullable=True,
    )
    # Client hints for the sessions list only — never an authorization input.
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
