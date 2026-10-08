"""Platform chat boxes: the credential a box boots with, and which org (if
any) a box is dedicated to.

- ``machine_credentials`` — one row per credential a platform admin minted for
  a named box. It carries everything registration needs to know that the box
  itself cannot be trusted to say: the catalog row (kind + size, so the true
  cost is the instance's real price), the region, the label, and the tenancy
  (``pool`` or ``dedicated``). The raw secret is shown once at mint; only its
  keyed digest is stored. ``machine_id`` is set when the box claims it: from
  then on the credential IS that machine, and a heartbeat on it for any other
  machine is refused. Revoking it ends the box's standing at once — its next
  heartbeat is a 401 and the meter reaps the row by lost heartbeat.
- ``org_compute_assignments`` — at most one row per org: the dedicated box
  that serves it, and whether its chats may fall back to the pool while that
  box is down (off by default: an enterprise org's chats run on its own box
  or wait). A partial unique index on ``machine_id`` is what makes "a dedicated
  box never serves another org" a fact of the schema rather than of a check
  in one route.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.compute import DEDICATED_TENANCY, PERSONAL_TENANCY, POOL_TENANCY

#: What a platform admin may mint from the console.
PLATFORM_TENANCIES: tuple[str, ...] = (POOL_TENANCY, DEDICATED_TENANCY)
#: Every tenancy a machine credential may carry: the platform's, and a
#: person's own box registered through the device flow (minted in the
#: person's org, ``created_by`` the person it serves).
MACHINE_CREDENTIAL_TENANCIES: tuple[str, ...] = (*PLATFORM_TENANCIES, PERSONAL_TENANCY)
MACHINE_LABEL_MAX = 128
MACHINE_REGION_MAX = 32


class MachineCredential(Base):
    __tablename__ = "machine_credentials"
    __table_args__ = (
        CheckConstraint(
            f"tenancy IN {MACHINE_CREDENTIAL_TENANCIES}", name="ck_machine_credentials_tenancy"
        ),
        Index("ix_machine_credentials_org_team_id", "org_team_id"),
        Index("ix_machine_credentials_machine_id", "machine_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # HMAC-SHA256(pepper, raw_secret); the raw secret is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(MACHINE_LABEL_MAX), nullable=False)
    # The operator org — the org of the admin who minted it. The box's service
    # principal belongs to this org; it is never the org the box serves.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # The catalog row the box registers as: its provider kind and size, and so
    # the true cost recorded per minute.
    machine_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("compute_machine_types.id", ondelete="RESTRICT"), nullable=False
    )
    region: Mapped[str] = mapped_column(
        String(MACHINE_REGION_MAX), nullable=False, server_default=""
    )
    tenancy: Mapped[str] = mapped_column(String(16), nullable=False)
    # The machine that claimed this credential; NULL until the box registers.
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("compute_allocations.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgComputeAssignment(Base):
    __tablename__ = "org_compute_assignments"
    __table_args__ = (Index("ux_org_compute_assignments_machine_id", "machine_id", unique=True),)

    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("compute_allocations.id", ondelete="CASCADE"), nullable=False
    )
    # Whether the org's chats may run on the pool while its box is down.
    fallback_to_pool: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "MACHINE_CREDENTIAL_TENANCIES",
    "MACHINE_LABEL_MAX",
    "MACHINE_REGION_MAX",
    "PLATFORM_TENANCIES",
    "MachineCredential",
    "OrgComputeAssignment",
]
