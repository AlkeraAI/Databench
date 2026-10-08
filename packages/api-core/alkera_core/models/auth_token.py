"""AuthToken model — server-side registry of issued session/CLI tokens.

One row per minted JWT, keyed by its `jti`. JWT decode/verify stays stateless
(`alkera_core.auth.tokens`); this table is the revocation + audit layer on top:
it lets a specific token be revoked (logout / "this device"), supports listing
a user's active sessions, and — together with `User.token_epoch` — backs the
"revoke-all" lever.

``refresh_family_id`` names the browser session (refresh family) a session
token was minted in, so ending the family revokes every access token in it.

``org_team_id`` and ``membership_id`` name the org and membership the token was
minted for (NULL on rows registered before tokens were org-bound), so the
sessions list can show the org and a membership's tokens can be found.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models._enums import TokenType


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    __table_args__ = (
        Index("ix_auth_tokens_membership", "membership_id"),
        Index("ix_auth_tokens_refresh_family", "refresh_family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The JWT's `jti` claim (uuid4 hex). Unique — one registry row per token.
    jti: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_type: Mapped[TokenType] = mapped_column(
        Enum(
            TokenType,
            native_enum=False,
            length=16,
            name="token_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    # Mirrors the JWT's iat/exp (stored tz-aware). `expires_at` is indexed so
    # the prune job + the revocation cache can filter to live tokens cheaply.
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE", name="fk_auth_tokens_org"), nullable=True
    )
    membership_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("org_memberships.id", ondelete="CASCADE", name="fk_auth_tokens_membership"),
        nullable=True,
    )
    # The browser session (refresh family) this access token was minted in;
    # NULL for a CLI token. Ending the family revokes every token it links.
    refresh_family_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Human label for session listing (device / client). Optional, future use.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
