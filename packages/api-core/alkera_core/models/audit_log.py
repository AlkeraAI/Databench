"""Audit log — one row per successful Alkera-staff (support/admin) write action.

Every mutating request through `/admin/v1/...` that succeeds (2xx) is recorded
here by the `AuditedRoute` route class. Reads are not logged.

Actor fields are denormalized *snapshots* (email + role captured at action time),
not just a foreign key, so an entry stays meaningful even after the actor is
renamed or deleted (`actor_id` is `ON DELETE SET NULL`). The row is immutable —
append-only, no update/delete surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The acting staff user. SET NULL (not CASCADE): the trail outlives the user.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Snapshots taken at action time — survive a later rename/delete of the actor.
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")
    # "alkera_support" | "alkera_admin" at the time of the action. Plain string
    # (not the enum / a FK) precisely because it is history, not current state.
    actor_platform_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The route's function name, e.g. "create_org", "grant_credits", "set_platform_role".
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    # The resolved request path (with ids), e.g. "/admin/v1/users/<uuid>/platform_role".
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    # What the action was done TO, named at action time ("Acme (org)",
    # "ana@x.io", a machine's name) — a snapshot, so a deleted org still reads.
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # {"path_params": {...}, "body": {...redacted...}} — secrets scrubbed before write.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
