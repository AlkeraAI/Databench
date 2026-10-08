"""Round-trip + forward-compat behavior of the test-catalog schemas."""

from __future__ import annotations

import json

import pytest
from alkera_core.schemas.testing import (
    GatePolicy,
    SandboxData,
    TestBinding,
    TestDefinition,
    TestKind,
    TestSeverity,
    TestSpec,
)
from alkera_core.versioning import VersionedModel


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(TestSpec(), id="spec-defaults"),
        pytest.param(
            TestSpec(
                id="test://duckdb/db/main.orders/amount/not_null/abc123def456",
                name="orders.tests.yml::orders.amount::not_null",
                definition=TestDefinition(kind=TestKind.NOT_NULL.value),
                params={"values": [1, 2]},
                refs=["duckdb://db/main.orders#amount", "duckdb://db/main.payments"],
                severity=TestSeverity.WARN.value,
                enabled=False,
                source="models/orders.tests.yml",
                surface="yaml",
            ),
            id="spec-populated",
        ),
        pytest.param(
            TestSpec(
                id="test://duckdb/db/main.orders/tenant_id/unique_combination/aabbccddeeff",
                name="orders.tests.yml::orders::unique_combination",
                definition=TestDefinition(kind=TestKind.UNIQUE_COMBINATION.value),
                params={"columns": ["tenant_id", "external_id"]},
                refs=["duckdb://db/main.orders#tenant_id", "duckdb://db/main.orders#external_id"],
            ),
            id="spec-unique-combination",
        ),
        pytest.param(
            TestSpec(
                id="test://duckdb/db/main.orders/amount/not_null/ffeeddccbbaa",
                name="orders.tests.yml::orders.amount::not_null(graduated)",
                definition=TestDefinition(kind=TestKind.NOT_NULL.value),
                refs=["duckdb://db/main.orders#amount"],
                warn_if="> 10",
                error_if="> 100",
            ),
            id="spec-graduated-thresholds",
        ),
        pytest.param(
            TestSpec(
                id="test://duckdb/db/main.orders/unit/00ffee11dd22",
                name="orders.tests.yml::orders::unit::rounds_amounts_down",
                definition=TestDefinition(kind=TestKind.UNIT.value),
                params={
                    "given": [
                        {
                            "input": "duckdb://db/main.raw_orders",
                            "rows": [
                                {"ID": 1, "AMOUNT_CENTS": 199, "STATUS": "completed"},
                                {"ID": 2, "AMOUNT_CENTS": None, "STATUS": None},
                            ],
                        }
                    ],
                    "expect": {"rows": [{"ORDER_ID": 1, "AMOUNT": 1.0, "ROUNDED": True}]},
                },
                refs=["duckdb://db/main.orders", "duckdb://db/main.raw_orders"],
            ),
            id="spec-unit-given-expect",
        ),
        pytest.param(
            TestBinding(
                test_id="test://x/y/not_null/0011aabbccdd",
                target_urn="duckdb://db/main.t#c",
                table_urn="duckdb://db/main.t",
                severity=TestSeverity.BLOCKING.value,
                enabled=True,
                spec_hash="deadbeef",
            ),
            id="binding",
        ),
        pytest.param(
            GatePolicy(
                enabled=False,
                mode="informational",
                severity={"NOT_NULL_TO_NULLABLE": TestSeverity.BLOCKING.value},
                ignore_only={"TYPE_WIDENED": ["duckdb://db/main.scratch_*"]},
                force=["TABLE_DROPPED"],
            ),
            id="policy",
        ),
        pytest.param(GatePolicy(sandbox_data=SandboxData.CLONE.value), id="policy-clone-sandbox"),
    ],
)
def test_round_trips(model: VersionedModel) -> None:
    reloaded = type(model).model_validate(json.loads(model.model_dump_json()))
    assert reloaded == model


def test_unknown_fields_from_a_newer_writer_survive_round_trip() -> None:
    payload = TestSpec(refs=["duckdb://db/main.t#c"]).model_dump(mode="json")
    payload["future_field"] = {"nested": True}
    reloaded = TestSpec.model_validate(payload)
    dumped = reloaded.model_dump(mode="json")
    assert dumped["future_field"] == {"nested": True}


def test_models_are_not_collected_as_pytest_tests() -> None:
    """The Test*-named models must carry the pytest collision guard."""
    for cls in (TestDefinition, TestSpec, TestBinding, TestKind):
        assert getattr(cls, "__test__", True) is False
