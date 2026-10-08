"""The customer rate of an org machine, from its offering."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.compute.pricing import (
    MINUTES_PER_MONTH,
    OfferingPricingError,
    compute_rate,
    storage_rate,
    true_storage_cost,
)
from alkera_core.models.compute import ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering


def _offering(**fields: object) -> ComputeOffering:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "machine_type_id": uuid.uuid4(),
        "name": "A100 80 GB",
        "pricing_mode": "pass_through",
        "markup_bps": 0,
        "fixed_rate_per_minute_nanos": None,
        "storage_gb_default": 50,
        "storage_gb_max": 500,
        "storage_rate_per_gb_month_nanos": 0,
        "audience": "all",
    }
    values.update(fields)
    return ComputeOffering(**values)


def _type(price: int) -> ComputeMachineType:
    return ComputeMachineType(
        provider="runpod",
        provider_type_id="t",
        display_name="t",
        provider_price_per_minute_nanos=price,
    )


@pytest.mark.parametrize(
    ("offering", "price", "rate"),
    [
        pytest.param(
            _offering(pricing_mode="fixed", fixed_rate_per_minute_nanos=7_000_000),
            123_456_789,
            7_000_000,
            id="fixed-ignores-the-provider-price",
        ),
        pytest.param(
            _offering(pricing_mode="fixed", fixed_rate_per_minute_nanos=0),
            50_000_000,
            0,
            id="fixed-at-zero-is-free",
        ),
        pytest.param(_offering(), 20_000_000, 20_000_000, id="pass-through-at-cost"),
        pytest.param(
            _offering(markup_bps=2_500), 20_000_000, 25_000_000, id="pass-through-plus-25-percent"
        ),
        # 333 * 1.0001 = 333.0333: a markup that does not divide rounds up,
        # never down to the provider's price.
        pytest.param(_offering(markup_bps=1), 333, 334, id="markup-rounds-up"),
        pytest.param(_offering(markup_bps=10_000), 333, 666, id="markup-that-divides-is-exact"),
        pytest.param(_offering(markup_bps=500), 0, 0, id="a-free-provider-price-stays-free"),
    ],
)
def test_compute_rate(offering: ComputeOffering, price: int, rate: int) -> None:
    assert compute_rate(offering, _type(price)) == rate


def test_a_fixed_offering_without_a_rate_refuses_to_price() -> None:
    with pytest.raises(OfferingPricingError):
        compute_rate(_offering(pricing_mode="fixed"), _type(1_000))


@pytest.mark.parametrize(
    ("gb", "per_gb_month", "per_minute"),
    [
        pytest.param(100, 0, 0, id="no-storage-price"),
        pytest.param(0, 100_000_000, 0, id="zero-storage"),
        pytest.param(1, MINUTES_PER_MONTH, 1, id="one-gb-at-exactly-a-nano-a-minute"),
        # 1 GB at 1 nano a month is a fraction of a nano a minute: it rounds up.
        pytest.param(1, 1, 1, id="a-fraction-of-a-nano-rounds-up"),
        pytest.param(100, 70_000_000, 162_038, id="hundred-gb-at-7-cents"),
        pytest.param(
            10_000, 70_000_000, 16_203_704, id="large-storage-has-no-overflow-or-truncation"
        ),
    ],
)
def test_storage_rate(gb: int, per_gb_month: int, per_minute: int) -> None:
    offering = _offering(storage_rate_per_gb_month_nanos=per_gb_month)
    assert storage_rate(offering, gb) == per_minute
    # Storage is passed through: what it costs us is what it is sold for.
    assert true_storage_cost(offering, gb) == per_minute


def test_storage_rate_refuses_a_negative_size() -> None:
    with pytest.raises(ValueError, match="negative"):
        storage_rate(_offering(storage_rate_per_gb_month_nanos=1), -1)
