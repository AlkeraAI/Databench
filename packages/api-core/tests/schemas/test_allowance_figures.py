"""A figure field on a cap or allowance route takes a plain non-negative integer
with a stated maximum; a USD amount takes a plain decimal string. What is not
that shape is refused as written — never read as a different figure, never
stored past what the column holds."""

from __future__ import annotations

import pytest
from alkera_core.schemas.tenancy.allocations import TeamAllocationUpdate
from alkera_core.schemas.tenancy.storage import OrgStorageLimitUpdate, StorageLimitUpdate
from alkera_core.units import MAX_CEILING_BYTES
from pydantic import BaseModel, ValidationError

BUDGET_BOUND = "A budget can be at most $1,000,000,000 per cycle."
STORAGE_BOUND = "A storage limit can be at most 1000 PB."
NOT_PLAIN = "Enter a plain amount in USD, like 12.50 — digits and a point only."


def _refusal(model: type[BaseModel], raw: str) -> tuple[str, str]:
    with pytest.raises(ValidationError) as info:
        model.model_validate_json(raw)
    error = info.value.errors()[0]
    return error["type"], error["msg"]


@pytest.mark.parametrize(
    "model",
    [TeamAllocationUpdate, StorageLimitUpdate, OrgStorageLimitUpdate],
    ids=lambda m: m.__name__,
)
def test_a_storage_figure_is_bounded_and_plain(model: type[BaseModel]) -> None:
    assert _refusal(model, '{"limit_bytes": 1e3}')[0] == "int_type"
    assert _refusal(model, '{"limit_bytes": -1}')[0] == "greater_than_equal"
    assert _refusal(model, f'{{"limit_bytes": {MAX_CEILING_BYTES + 1}}}') == (
        "storage_too_large",
        STORAGE_BOUND,
    )
    assert _refusal(model, '{"limit_bytes": 10000000000000000000}') == (
        "storage_too_large",
        STORAGE_BOUND,
    )
    assert model.model_validate_json(f'{{"limit_bytes": {MAX_CEILING_BYTES}}}')


def test_an_explicit_unlimited_override_is_still_null() -> None:
    assert OrgStorageLimitUpdate.model_validate_json('{"limit_bytes": null}').limit_bytes is None


@pytest.mark.parametrize(
    "model",
    [TeamAllocationUpdate],
    ids=lambda m: m.__name__,
)
@pytest.mark.parametrize(
    ("amount", "kind", "msg"),
    [
        pytest.param("-5", "usd_not_plain", NOT_PLAIN, id="negative"),
        pytest.param("+5", "usd_not_plain", NOT_PLAIN, id="signed"),
        pytest.param("1e3", "usd_not_plain", NOT_PLAIN, id="exponent"),
        pytest.param("abc", "usd_not_plain", NOT_PLAIN, id="letters"),
        pytest.param("", "usd_not_plain", NOT_PLAIN, id="empty"),
        pytest.param("1,000", "usd_not_plain", NOT_PLAIN, id="thousands-separator"),
        pytest.param("$5", "usd_not_plain", NOT_PLAIN, id="currency-mark"),
        pytest.param("5.", "usd_not_plain", NOT_PLAIN, id="trailing-point"),
        pytest.param(".5", "usd_not_plain", NOT_PLAIN, id="leading-point"),
        pytest.param("5.0000000001", "usd_not_plain", NOT_PLAIN, id="past-nano-precision"),
        pytest.param("1000000000.01", "budget_too_large", BUDGET_BOUND, id="past-the-bound"),
    ],
)
def test_a_usd_amount_is_read_exactly_as_typed(
    model: type[BaseModel], amount: str, kind: str, msg: str
) -> None:
    with pytest.raises(ValidationError) as info:
        model.model_validate({"amount_usd": amount})
    error = info.value.errors()[0]
    assert (error["type"], error["msg"]) == (kind, msg)


@pytest.mark.parametrize(
    "model",
    [TeamAllocationUpdate],
    ids=lambda m: m.__name__,
)
@pytest.mark.parametrize("amount", ["0", "5", "12.50", "0.000000001", "1000000000", " 7.25 "])
def test_a_plain_usd_amount_is_admitted(model: type[BaseModel], amount: str) -> None:
    assert model.model_validate({"amount_usd": amount}).amount_usd == amount.strip()  # type: ignore[attr-defined]
