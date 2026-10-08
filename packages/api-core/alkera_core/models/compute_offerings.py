"""The machines Alkera sells: ``compute_offerings`` and who may buy them.

- ``compute_offerings`` — a platform table. One row is something an org may
  buy: a machine type (the provider's hardware shape), the name the buy dialog
  shows, how the customer rate is set (``pass_through``: the provider's price
  plus ``markup_bps``; ``fixed``: ``fixed_rate_per_minute_nanos``), the storage
  sizes on offer and their price, and who may buy it (``all`` orgs, only
  ``enterprise`` orgs, or the orgs ``listed`` in ``compute_offering_orgs``).
  Platform admins write it; an offering is retired (``retired_at``), never
  deleted, because org machines and invoices keep naming it. ``purchasable =
  false`` keeps it serving the machines already bought from it while hiding it
  from the buy dialog.
- ``compute_offering_orgs`` — the orgs a ``listed`` offering is sold to.

Neither table is a tenant table: an offering holds no tenant content, so it
carries no row-security policy. The customer rate is derived by
:func:`alkera_core.compute.pricing.compute_rate`; ``markup_bps`` and the
provider price never reach a tenant response.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

PricingMode = Literal["pass_through", "fixed"]
#: The customer rate is the provider's price plus a markup in basis points.
PRICING_PASSTHROUGH: PricingMode = "pass_through"
#: The customer rate is a set figure, whatever the provider charges.
PRICING_FIXED: PricingMode = "fixed"
PRICING_MODES: tuple[str, ...] = (PRICING_PASSTHROUGH, PRICING_FIXED)

OfferingAudience = Literal["all", "enterprise", "listed"]
#: Every org may buy it.
AUDIENCE_ALL: OfferingAudience = "all"
#: Only orgs on an enterprise plan may buy it.
AUDIENCE_ENTERPRISE: OfferingAudience = "enterprise"
#: Only the orgs in ``compute_offering_orgs`` may buy it.
AUDIENCE_LISTED: OfferingAudience = "listed"
OFFERING_AUDIENCES: tuple[str, ...] = (AUDIENCE_ALL, AUDIENCE_ENTERPRISE, AUDIENCE_LISTED)

OFFERING_NAME_MAX = 128
OFFERING_DESCRIPTION_MAX = 512
OFFERING_REGION_MAX = 32


class ComputeOffering(Base):
    __tablename__ = "compute_offerings"
    __table_args__ = (
        CheckConstraint(
            f"pricing_mode IN {PRICING_MODES}", name="ck_compute_offerings_pricing_mode"
        ),
        CheckConstraint("markup_bps >= 0", name="ck_compute_offerings_markup_nonnegative"),
        CheckConstraint(
            "fixed_rate_per_minute_nanos IS NULL OR fixed_rate_per_minute_nanos >= 0",
            name="ck_compute_offerings_fixed_rate_nonnegative",
        ),
        CheckConstraint(
            "pricing_mode <> 'fixed' OR fixed_rate_per_minute_nanos IS NOT NULL",
            name="ck_compute_offerings_fixed_rate",
        ),
        CheckConstraint("storage_gb_default > 0", name="ck_compute_offerings_storage_default"),
        CheckConstraint(
            "storage_gb_max >= storage_gb_default", name="ck_compute_offerings_storage_max"
        ),
        CheckConstraint(
            "storage_rate_per_gb_month_nanos >= 0",
            name="ck_compute_offerings_storage_rate_nonnegative",
        ),
        CheckConstraint(f"audience IN {OFFERING_AUDIENCES}", name="ck_compute_offerings_audience"),
        CheckConstraint(
            "idle_stop_minutes_default IS NULL OR idle_stop_minutes_default >= 5",
            name="ck_compute_offerings_idle_stop_default",
        ),
        Index("ix_compute_offerings_machine_type_id", "machine_type_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    machine_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "compute_machine_types.id",
            ondelete="RESTRICT",
            name="fk_compute_offerings_machine_type_id",
        ),
        nullable=False,
    )
    # What the machine is sold as ("A100 80 GB"): the machine's spec line,
    # never the name an org gives its machine.
    name: Mapped[str] = mapped_column(String(OFFERING_NAME_MAX), nullable=False)
    description: Mapped[str] = mapped_column(
        String(OFFERING_DESCRIPTION_MAX), nullable=False, server_default=""
    )
    pricing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # The pass-through markup over the provider's price, in basis points.
    # Platform-visibility only.
    markup_bps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The customer rate of a ``fixed`` offering, integer nano-USD per minute.
    fixed_rate_per_minute_nanos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_gb_default: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_gb_max: Mapped[int] = mapped_column(Integer, nullable=False)
    # The volume price per GB per 30-day month, billed while the provider holds
    # the volume (running and stopped).
    storage_rate_per_gb_month_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    region: Mapped[str] = mapped_column(
        String(OFFERING_REGION_MAX), nullable=False, server_default=""
    )
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    purchasable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # The idle stop a new machine starts with; NULL never stops one for idling.
    idle_stop_minutes_default: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_compute_offerings_created_by"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComputeOfferingOrg(Base):
    """One org a ``listed`` offering is sold to."""

    __tablename__ = "compute_offering_orgs"
    __table_args__ = (Index("ix_compute_offering_orgs_org_team_id", "org_team_id"),)

    offering_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "compute_offerings.id",
            ondelete="CASCADE",
            name="fk_compute_offering_orgs_offering_id",
        ),
        primary_key=True,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_compute_offering_orgs_org_team_id"),
        primary_key=True,
    )


__all__ = [
    "AUDIENCE_ALL",
    "AUDIENCE_ENTERPRISE",
    "AUDIENCE_LISTED",
    "OFFERING_AUDIENCES",
    "OFFERING_DESCRIPTION_MAX",
    "OFFERING_NAME_MAX",
    "OFFERING_REGION_MAX",
    "PRICING_FIXED",
    "PRICING_MODES",
    "PRICING_PASSTHROUGH",
    "ComputeOffering",
    "ComputeOfferingOrg",
    "OfferingAudience",
    "PricingMode",
]
