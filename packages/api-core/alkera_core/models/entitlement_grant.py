"""The SaaS-side issuance ledger for signed entitlement tokens.

One row per minted ``ALKERA_ENTITLEMENTS`` token. Unlike proxy tokens the FULL
token string is stored: an entitlement token confers zero authority on Alkera's
systems (it is verified only inside the customer's own deployment, bound to
their slug), and staff need to re-display / resend it for support — the row IS
the audit trail. The table exists in every DB (shared metadata) but only
Alkera's SaaS ever writes it.

Offline verification means there is NO revocation: an issued token verifies
until its expiry no matter what happens server-side. ``superseded_at`` is
bookkeeping (which token should be live; support declines to re-send an old
one) — real enforcement is always the expiry date.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Identity, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class EntitlementGrant(Base):
    __tablename__ = "entitlement_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Attribution slug embedded in the token payload (shows in the customer's
    # boot log; names the licensee if a token ever leaks).
    customer_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    # DB-assigned issuance serial — the payload's "n". Strictly increasing
    # across ALL grants, so support can always tell which token is newest.
    serial: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    # The raw feature bitmask embedded in the token (bit 0 = BYOK).
    features: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    # The full signed token, re-displayable by design (see module docstring).
    token: Mapped[str] = mapped_column(Text, nullable=False)
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Bookkeeping only — an offline-verified token cannot be revoked. Minting a
    # new grant auto-supersedes the org's prior active ones.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
