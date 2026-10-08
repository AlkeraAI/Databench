"""Account lifecycle: a person's data export requests and account deletion requests.

An export request is one archive of everything about one person, built by the
worker (``account.export``), stored under ``account-exports/`` in the Files
bucket and downloadable by its owner, with the emailed link, until it expires.

A deletion request schedules the erasure of one identity after a grace window.
It is never deleted itself: once completed it is the record that the erasure
happened, holding the plan the person confirmed and the certificate the erasure
wrote. Neither JSON document carries a name or an address (ids and counts
only), so the record outlives the person without keeping anything personal.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: Where an export is in its life. ``queued`` until the worker picks it up,
#: ``running`` while it builds, ``ready`` once downloadable, ``expired`` once the
#: archive is gone, ``failed`` when the build gave up.
EXPORT_STATUSES: tuple[str, ...] = ("queued", "running", "ready", "failed", "expired")

#: Where a deletion is in its life. A scheduled request is the only live one.
DELETION_STATUSES: tuple[str, ...] = ("scheduled", "cancelled", "completed")

#: Who asked: the person themselves, platform support acting on a request the
#: person made through another channel, or the re-erasure of an identity the
#: erasure ledger says was erased and a restore brought back.
REQUEST_SOURCES: tuple[str, ...] = ("self", "support", "restore")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AccountExportRequest(Base):
    __tablename__ = "account_export_requests"
    __table_args__ = (
        CheckConstraint(_in("status", EXPORT_STATUSES), name="ck_account_export_requests_status"),
        CheckConstraint(_in("source", REQUEST_SOURCES), name="ck_account_export_requests_source"),
        # The rate limit and the person's own list: their requests, newest first.
        Index("ix_account_export_requests_user_created", "user_id", "created_at"),
        # The sweep: exports a lost nudge left queued, and ready ones past expiry.
        Index("ix_account_export_requests_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_account_export_requests_user_id_users"),
        nullable=False,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_account_export_requests_requested_by_id_users",
        ),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="self")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: The archive's key under the Files bucket (``account-exports/...``).
    archive_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    archive_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: HMAC digest of the download token in the emailed link; the raw token is
    #: only ever in the email (``alkera_core.auth.token_hash``).
    download_token_hash: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AccountDeletionRequest(Base):
    __tablename__ = "account_deletion_requests"
    __table_args__ = (
        CheckConstraint(
            _in("status", DELETION_STATUSES), name="ck_account_deletion_requests_status"
        ),
        CheckConstraint(_in("source", REQUEST_SOURCES), name="ck_account_deletion_requests_source"),
        # At most one live request per person: a second "delete my account"
        # while one is scheduled is the same request, not another.
        Index(
            "uq_account_deletion_requests_user_scheduled",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'scheduled'"),
        ),
        # The sweep: scheduled requests whose grace has ended.
        Index(
            "ix_account_deletion_requests_due",
            "purge_after",
            postgresql_where=text("status = 'scheduled'"),
        ),
        Index("ix_account_deletion_requests_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="CASCADE", name="fk_account_deletion_requests_user_id_users"
        ),
        nullable=False,
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_account_deletion_requests_requested_by_id_users",
        ),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="self")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="scheduled")
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why a due erasure is waiting (a blocker that appeared during the grace
    #: window), and when the person was told. Cleared once it runs.
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The plan the person confirmed (``alkera_core.schemas.account.DeletionPlan``).
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: What the erasure did (``alkera_core.schemas.account.ErasureCertificate``).
    certificate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


__all__ = [
    "DELETION_STATUSES",
    "EXPORT_STATUSES",
    "REQUEST_SOURCES",
    "AccountDeletionRequest",
    "AccountExportRequest",
]
