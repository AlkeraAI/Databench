"""Signup-funnel telemetry — anonymous click → signup → paid conversion.

One row per visit to the external tier-signup link (``/start?plan=…&ref=…&utm_*``).
A ``visit_token`` is minted + cookied at the click; ``user_id`` is bound when that
visitor signs up, and the Stripe ids when they subscribe. This is TELEMETRY ONLY —
the recorded ``tier`` is what the marketing link *suggested*, never an entitlement:
provisioning always grants Free regardless, and a forged/mismatched token changes
nothing about what a user receives (asserted by a test). PII-minimal: only a coarse
referer host + length-capped, sanitized UTM/ref are stored, never a raw IP.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.db.base import Base


class SignupClick(Base):
    __tablename__ = "billing_signup_clicks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    visit_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # The tier/interval the marketing link suggested — telemetry, NOT entitlement.
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    interval: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_term: Mapped[str | None] = mapped_column(String(128), nullable=True)
    utm_content: Mapped[str | None] = mapped_column(String(128), nullable=True)
    referer_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Bound at signup (SET NULL if the user is later deleted — keep the click row).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Bound at purchase.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_billing_signup_clicks_user_id", "user_id"),
        Index("ix_billing_signup_clicks_created_at", "created_at"),
    )


# What erasing an account does to the columns here that name a person. Spelled
# beside the table so the two always load together.
dispositions.register(
    Disposition(
        "billing_signup_clicks",
        "user_id",
        Kind.DETACH,
        "marketing attribution; no retention need",
        step=dispositions.null_column("billing_signup_clicks", "user_id"),
    )
)
