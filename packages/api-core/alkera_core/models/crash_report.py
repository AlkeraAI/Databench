"""User-submitted crash reports.

One row per opt-in crash/error report a user chooses to send from the CLI,
daemon, VS Code extension, or web app. Reports are append-only history (never
mutated), so this is a plain `Base` model — `created_at` is the audit timestamp.

Payloads are redacted client-side AND again on the server (`alkera_core`'s
scrubber) before persistence: no secrets, no prompt/chat/file content.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base

if TYPE_CHECKING:
    from alkera_core.models.user import User


class CrashReport(Base):
    __tablename__ = "crash_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The reporter's org root team, denormalized for org-scoped triage queries.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Which surface produced the crash: "cli" | "daemon" | "extension" | "web" |
    # "backend" | "gateway".
    component: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Exception class / error name, when known.
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Human-readable summary / error message (already client-safe).
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Redacted stack trace, when available.
    stacktrace: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured, analytics-safe context (versions, counts, flags, ids).
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Free-form note the user added before sending.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # OS / arch string, e.g. "darwin/arm64".
    platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Recent log tail the user consented to include (redacted).
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the crash happened (client clock), distinct from when it was received.
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # --- Triage (set by platform staff in the admin dashboard) ---
    # When an admin marked this report read; NULL = unread. Indexed for the
    # unread filter.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Which admin marked it read. SET NULL on user delete — keep the report.
    read_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Two FKs to users — disambiguate each relationship explicitly.
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    read_by: Mapped[User | None] = relationship(foreign_keys=[read_by_user_id])
