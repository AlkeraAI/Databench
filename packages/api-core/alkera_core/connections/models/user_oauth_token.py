"""Backend-held per-user OAuth tokens for team Preconfigured Connections.

For CONFIDENTIAL per-user OAuth providers (e.g. BigQuery — the org registers a
Google client whose secret lives only on the backend), the code→token exchange
and every refresh run server-side. The member's refresh token therefore NEVER
leaves the backend: it rests here encrypted (``alkera_core.auth.secret_box``),
and the member's daemon receives only short-lived access tokens.

Public-client providers (Snowflake/Databricks built-in loopback clients) keep
their bundles on the member's machine and never touch this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.connections import CredentialState
from alkera_core.db.base import Base
from alkera_core.db.row_security import register_content_table

#: The three a member's backend-held grant can be in. ``revoked`` is
#: reserved: this slice deletes the row on sign-out rather than marking it.
_STATES = (
    "("
    + ",".join(
        f"'{s.value}'"
        for s in (CredentialState.present, CredentialState.needs_reauth, CredentialState.revoked)
    )
    + ")"
)


class UserOAuthToken(Base):
    __tablename__ = "user_oauth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "team_connection_id", name="uq_user_oauth_tokens_user_connection"
        ),
        CheckConstraint(f"state IN {_STATES}", name="ck_uot_state"),
        # The refresh sweep's whole query: the grants worth renewing, in expiry
        # order. A row the provider already refused is not one of them.
        Index("ix_uot_state_expires", "state", "expires_at"),
        Index("ix_user_oauth_tokens_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("team_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The org the row belongs to: the key the ``tenant_isolation`` row-level
    #: policy filters on. No Python default: the ``trg_user_oauth_tokens_org`` trigger fills it
    #: from its connection's org when a writer leaves it unset, refuses a value that
    #: disagrees, and refuses any change of it; the insert reads it back.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_user_oauth_tokens_org_team_id_teams"),
        nullable=False,
        server_default=FetchedValue(),
    )
    # Both encrypted at rest; the refresh token is never returned by ANY API.
    access_token_encrypted: Mapped[str] = mapped_column(String(8192), nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    # Epoch-derived expiry of the ACCESS token (refresh happens server-side on lease).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")

    #: ``present`` | ``needs_reauth``. A refresh the provider DEFINITIVELY
    #: refused writes ``needs_reauth`` and the sentence the member reads; a
    #: network blip, a 5xx or a timeout never touches it, because a provider
    #: that was merely unreachable has not said anything about this grant.
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="present")
    state_reason: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The last successful renewal — distinct from ``last_used_at``, which the
    #: automatic sweep deliberately leaves alone.
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


#: Held to the tenant content policy on its org, like every content table.
register_content_table("user_oauth_tokens", "org_team_id")

# What erasing an account does to the columns here that name a person. Spelled
# beside the table so the two always load together.
dispositions.register(
    Disposition(
        "user_oauth_tokens",
        "user_id",
        Kind.ERASE,
        "a personal connector credential",
        step=dispositions.delete_rows("user_oauth_tokens", "user_id"),
        order=10,
    )
)
