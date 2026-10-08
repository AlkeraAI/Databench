"""Per-org audit trail — one row per security-relevant action inside an org.

Distinct from the platform-staff ``AuditLog`` (which records ``/admin/v1`` route
mutations): this is ORG-scoped + EVENT-based, emitted by the domain (a budget
set, a proxy token minted/revoked, an SSO config change, a login), and read by an
ORG ADMIN of that org. Actor fields are snapshots (survive a rename/delete);
``detail`` is redacted before write. Append-only, immutable.

**Tamper-evident.** Each row is hash-linked to the one before it within its org:
``entry_hash = SHA-256(canonical(content) || prev_hash)``. Any in-place edit,
deletion, re-ordering, or insertion breaks the chain at the affected row, which
``org_audit_service.verify_chain`` detects. The chain is unkeyed, so a sufficiently
privileged attacker could recompute the whole forward chain — periodically export
the latest ``entry_hash`` checkpoint to an append-only/external store (e.g. a SIEM)
to also catch a full rewrite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class OrgAuditEvent(Base):
    __tablename__ = "org_audit_events"
    __table_args__ = (
        # The read path is always org-scoped and time-ordered (list pages, the
        # chain tail, verification) — one composite serves the seek + the sort.
        Index("ix_org_audit_events_org_created", "org_team_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The acting user. SET NULL (not CASCADE): the trail outlives the user.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")
    # A stable dotted action code, e.g. "billing.budget_set", "proxy_token.minted",
    # "sso.config_updated", "auth.login".
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # Free-text subject of the action (the affected user's email, a token label, …).
    target: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Extra context, secrets scrubbed before write.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # Tamper-evident hash chain (per org). ``entry_hash`` = SHA-256 over the row's
    # canonical content + ``prev_hash`` (the previous chained row's entry_hash, ""
    # at genesis). Nullable so rows written before the chain existed are simply
    # skipped by verification. Written by org_audit_service.record under a per-org
    # advisory lock so concurrent appends can't fork the chain.
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
