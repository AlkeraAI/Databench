"""Device authorization model (RFC 8628 OAuth 2.0 Device Authorization Grant).

One row per `alkera login` / VS Code "Sign in via Browser" attempt. The CLI
requests a `device_code` (high-entropy secret) + `user_code` (short, human-typed)
and polls the token endpoint; meanwhile the user approves in a browser, binding
the row to their account. The flow decouples the polling device from the auth
device — no loopback HTTP server, works over SSH / in containers / on Windows.

The raw `device_code` is NEVER stored — only its keyed HMAC hash, so a DB read
can't replay a poll. The `user_code` is stored verbatim (the SPA looks it up by
value); its low entropy is defended by a short TTL, status-scoped uniqueness, and
a per-session attempt cap, not by hashing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base
from alkera_core.models._enums import DeviceAuthStatus

if TYPE_CHECKING:
    from alkera_core.models.user import User


class DeviceAuthorization(Base):
    __tablename__ = "device_authorizations"
    __table_args__ = (
        # At most one PENDING row per user_code at a time. A recycled code after
        # expiry/approval is fine. Declared here so Alembic autogenerate sees it
        # and doesn't propose dropping the index the migration creates.
        Index(
            "uq_device_auth_pending_user_code",
            "user_code",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The GC sweeper filters on expires_at.
        Index("ix_device_auth_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # HMAC-SHA256 hex digest of the raw device_code (never the raw value). Unique
    # so the token endpoint does an O(1) indexed equality lookup.
    device_code_hash: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, index=True
    )
    # The short human-typed code (e.g. "WXYZ-1234"). Stored verbatim because the
    # SPA looks it up directly; brute force is mitigated by the partial-unique
    # pending index + TTL + the per-session attempt counter, NOT by hashing.
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)

    # NULL until an authenticated user approves; then bound to the approver.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # The org the approving browser session was in: the CLI token redeemed from
    # this row is minted for it. NULL (rows approved before) is the home org.
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_device_authorizations_org"),
        nullable=True,
    )
    # Echoed from the device/code request — identifies the client surface
    # ("alkera-cli" / "alkera-vscode") for the consent screen + audit.
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Mirrors the `interval` handed to the client; drives slow_down enforcement.
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[DeviceAuthStatus] = mapped_column(
        Enum(
            DeviceAuthStatus,
            native_enum=False,
            length=16,
            name="device_auth_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=DeviceAuthStatus.PENDING,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Last token-endpoint poll; a poll sooner than `interval_seconds` after this
    # yields slow_down.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Monotonic poll counter feeding the hard poll cap.
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    denied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set the instant the JWT is handed out; presence ⇒ a replay poll is rejected.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    user: Mapped[User | None] = relationship()
