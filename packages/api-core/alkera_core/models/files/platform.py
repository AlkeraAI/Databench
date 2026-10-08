"""The platform-scoped tables and the quarantine.

``file_platform`` and ``file_sweep_shards`` join ``file_stores`` as the three
Files tables with no tenant rows — the RLS exception listed by name in the
migration test. ``file_platform`` holds exactly one row: the restore generation
the runbook and the restore drill bump *before* the database is opened for
writes, so every lease epoch issued after a restore is above every epoch issued
before it, including the ones the restore lost.

``file_quarantine`` is a tenant table: it names an org's row or object that a
sweeper refused to act on, so nothing is silently dropped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: What kind of thing was quarantined, so the operator knows which sweeper
#: refused and where to look.
QUARANTINE_KINDS: tuple[str, ...] = (
    "node",
    "version",
    "object",
    "pack",
    "upload_session",
    "stage_job",
)


class FilePlatform(Base):
    """One row, deployment-wide. Not a tenant table: no ``org_team_id``, no RLS."""

    __tablename__ = "file_platform"
    __table_args__ = (
        # The single-row invariant, stated where it cannot be bypassed: a
        # second row is a constraint violation, not a convention.
        CheckConstraint("id = 1", name="ck_file_platform_single_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Incremented by the restore runbook and the restore drill before writes
    #: are allowed; the high half of every lease epoch.
    restore_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FileSweepShard(Base):
    """One shard of the GC sweep, and the lease that makes one mutator per
    shard structural. Not a tenant table."""

    __tablename__ = "file_sweep_shards"
    __table_args__ = (
        # Serves the janitor's claim: the shards whose lease has lapsed.
        Index("ix_file_sweep_shards_expires_at", "expires_at"),
    )

    shard: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The worker instance holding the shard; NULL when nobody does.
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where a killed sweep resumes from — a sweep is never restarted.
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sweep_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FileQuarantine(Base):
    """Something a sweeper refused to act on, kept with its reason."""

    __tablename__ = "file_quarantine"
    __table_args__ = (
        # Serves the operator's queue: an org's quarantine by kind, oldest
        # first, and the janitor's 30 d cleanup after resolution.
        Index("ix_file_quarantine_org_kind_first_seen", "org_team_id", "kind", "first_seen_at"),
        # Serves the "is this thing already quarantined" lookup on the sweep's
        # hot path, which is by the referenced row rather than by org.
        Index("ix_file_quarantine_ref_id", "ref_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(
            *QUARANTINE_KINDS,
            name="ck_file_quarantine_kind",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: The offending row or object, spelled as text because it can be a uuid or
    #: a store key depending on ``kind``. No FK: the referent may be gone.
    ref_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "QUARANTINE_KINDS",
    "FilePlatform",
    "FileQuarantine",
    "FileSweepShard",
]
