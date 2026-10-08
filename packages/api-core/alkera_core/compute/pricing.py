"""What an org machine costs its org, per minute, from the offering it was
bought from.

Pure: no I/O. Every figure is integer nano-USD and every division rounds UP,
so a rate that does not divide evenly never bills less than the rule says.
The rates are pinned on the allocation at each start and wake; nothing here
re-rates a running machine.
"""

from __future__ import annotations

from alkera_core.models.compute import ComputeMachineType
from alkera_core.models.compute_offerings import PRICING_FIXED, ComputeOffering

#: Basis points in one whole.
BPS = 10_000
#: Minutes in the 30-day month a storage price is quoted per.
MINUTES_PER_MONTH = 30 * 24 * 60


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


class OfferingPricingError(ValueError):
    """An offering whose row cannot price a minute (a fixed offering with no rate)."""


def compute_rate(offering: ComputeOffering, machine_type: ComputeMachineType) -> int:
    """The customer rate per minute: the fixed rate of a ``fixed`` offering,
    else the provider's price plus the markup, rounded up."""
    if offering.pricing_mode == PRICING_FIXED:
        if offering.fixed_rate_per_minute_nanos is None:
            raise OfferingPricingError(f"fixed offering {offering.id} carries no rate")
        return offering.fixed_rate_per_minute_nanos
    return _ceil_div(
        machine_type.provider_price_per_minute_nanos * (BPS + offering.markup_bps), BPS
    )


def storage_rate(offering: ComputeOffering, storage_gb: int) -> int:
    """The customer storage price per minute for a volume of ``storage_gb``."""
    if storage_gb < 0:
        raise ValueError("storage_gb must not be negative")
    return _ceil_div(storage_gb * offering.storage_rate_per_gb_month_nanos, MINUTES_PER_MONTH)


def true_storage_cost(offering: ComputeOffering, storage_gb: int) -> int:
    """What the volume costs us per minute. Storage is priced pass-through, so
    today this is the customer storage rate; it is its own function so a
    marked-up storage price changes one place."""
    return storage_rate(offering, storage_gb)


__all__ = [
    "BPS",
    "MINUTES_PER_MONTH",
    "OfferingPricingError",
    "compute_rate",
    "storage_rate",
    "true_storage_cost",
]
