"""The durable hourly window behind the strict rate-limit classes.

The per-route token buckets are in-process by design, so their fleet-wide
ceiling is ``limit x tasks``. For the two classes where that multiplication is
not acceptable — a credential attempt and a credential mint — every task also
counts the hour in this table, one row per ``(class, key, hour)``, incremented
by a single ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent attempts can
never lose a count. Rows older than the window serve no check any more and
are purged opportunistically by the writer.

``key`` is what the class counts against (an account, a principal, a caller
address) and never the credential itself: an email is stored lowercased, a
token or device code only as a digest.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class RateLimitWindow(Base):
    __tablename__ = "rate_limit_windows"
    __table_args__ = (Index("ix_rate_limit_windows_window_start", "window_start"),)

    rate_class: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
