"""The brute-force lockout counter for an address that has no usable account.

A real account keeps its counter on its own `users` row. That leaves an
asymmetry an attacker can read: spray six passwords at an address and a real
one starts answering `429 account_locked` while an unknown one answers `401`
forever — six requests per address and you know who has an Alkera account. The
login route already runs a decoy KDF so latency cannot leak existence, and the
password-reset route answers a constant message; the lockout undid both on the
same surface.

This table gives every other address the same counter, so the whole observable
schedule — when the 429 starts, how long it lasts, when the streak restarts —
is identical either way. It stores only a digest of the submitted identifier:
the point is to reproduce a schedule, not to keep a list of addresses that
people have tried to sign in as.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class LoginLockout(Base):
    __tablename__ = "login_lockouts"
    #: The nightly prune leads on ``last_failed_at`` — a counter is spent only
    #: once it is past its window, and its cool-off is then a recheck on the
    #: rows that range already fetched. Only the leading column earns an index:
    #: this table is written on every failed sign-in, so an index no query
    #: leads on would be write cost the spray pays and nothing reads.
    __table_args__ = (Index("ix_login_lockouts_last_failed_at", "last_failed_at"),)

    #: SHA-256 of the normalized identifier, peppered with the deployment's token
    #: hash pepper — so a stolen dump cannot be scanned for a given address.
    identifier_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
