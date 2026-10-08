"""Deployment health snapshot (self-hosted ops dashboard).

A 5-minute schedule on the worker probes everything a self-hosted operator cares
about (Postgres/Temporal/gateway/providers/SMTP/migrations/entitlement…) and
full-replaces this latest-only snapshot; the org-admin Health tab reads it. Two
tables: the per-check rows and a singleton run-metadata row whose
``last_scheduled_at`` is the schedule-liveness signal (a dead worker, or a schedule
that never synced, leaves it stale). Latest-only by design — history belongs to
real observability; manual runs are captured in the org audit log.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

_STATUSES = "('ok','warn','fail','skipped')"
_TRIGGERS = "('scheduled','manual')"


class DeploymentHealthCheck(Base):
    __tablename__ = "deployment_health_checks"
    __table_args__ = (
        CheckConstraint(f"status IN {_STATUSES}", name="ck_deployment_health_checks_status"),
        CheckConstraint(f"trigger IN {_TRIGGERS}", name="ck_deployment_health_checks_trigger"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    check_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    # NULL = an instance-level check; set = the org the check pertains to (a
    # provider probe). Org admins only ever see instance rows + their own org's.
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)


class DeploymentHealthRun(Base):
    __tablename__ = "deployment_health_runs"

    # Singleton row (id=1).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    last_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Bumped ONLY by a scheduled run — the schedule-liveness signal (stale ⇒ the
    # worker is down or its schedules never synced, and the GET overlays a fail
    # without needing a run).
    last_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
