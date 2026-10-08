"""Round-trip, defaults, bounds and forward-compat behaviour of the drain shapes,
plus the guarantee that they survive Temporal's pydantic payload converter (the
one every client, worker and test environment is built with)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_core.schemas.temporal import (
    DrainInput,
    DrainOutcome,
    DrainReport,
    SweepInput,
    ToolCallActivityInput,
)
from alkera_core.versioning import VersionedModel
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "temporal"


@pytest.mark.parametrize(
    ("model", "version"),
    [
        pytest.param(DrainInput, "1.0.0", id="DrainInput"),
        pytest.param(DrainOutcome, "1.1.0", id="DrainOutcome"),
        pytest.param(DrainReport, "1.0.0", id="DrainReport"),
        pytest.param(SweepInput, "1.0.0", id="SweepInput"),
        pytest.param(ToolCallActivityInput, "1.0.0", id="ToolCallActivityInput"),
    ],
)
def test_history_shapes_are_versioned_models(model: type[VersionedModel], version: str) -> None:
    assert issubclass(model, VersionedModel)
    assert model.SCHEMA_VERSION == version
    assert model.model_config.get("extra") == "allow"


def test_sweep_input_defaults_to_the_wall_clock() -> None:
    assert SweepInput().now is None


def test_sweep_input_pinned_clock_round_trips_with_its_timezone() -> None:
    now = datetime(2020, 6, 1, tzinfo=UTC)
    dumped = SweepInput(now=now).model_dump(mode="json")
    assert dumped["now"] == "2020-06-01T00:00:00Z"
    assert SweepInput.model_validate(dumped).now == now


def test_sweep_input_refuses_a_naive_clock() -> None:
    """The rows a sweep compares against carry a timezone; a naive instant would
    compare wrong (or not at all) instead of pinning anything."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        SweepInput(now=datetime(2020, 6, 1))


def test_drain_input_defaults_match_the_loop_contract() -> None:
    i = DrainInput()
    assert i.now is None
    assert i.limit == 100
    assert i.max_passes == 8
    assert i.locked_retries == 5
    assert i.locked_retry_seconds == 2.0


def test_drain_outcome_and_report_default_to_nothing_done() -> None:
    assert DrainOutcome() == DrainOutcome(
        processed=0, applied=0, failed=0, skipped_locked=False, skipped_unconfigured=False
    )
    assert DrainReport() == DrainReport(passes=0, processed=0, emails=0, locked_retries=0)


def test_an_outcome_written_before_the_split_reads_its_total_as_applied() -> None:
    """The earlier writer reported one number and the loop went again on it, so
    a run replaying such a pass reads it exactly as the loop did then."""
    loaded = DrainOutcome.model_validate({"schema_version": "1.0.0", "processed": 7})
    assert (loaded.processed, loaded.applied, loaded.failed) == (7, 7, 0)
    assert loaded.schema_version == "1.1.0"


def test_the_earlier_writers_recorded_full_page_still_fills_a_page() -> None:
    fixture = FIXTURE_ROOT / "v1_0_0" / "drain_outcome" / "full_page.json"
    loaded = DrainOutcome.model_validate(json.loads(fixture.read_text()))
    assert loaded.applied == 100 == loaded.processed
    assert loaded.failed == 0


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param({"applied": 3, "failed": 2}, (5, 3, 2), id="both-halves"),
        pytest.param({"applied": 3}, (3, 3, 0), id="applied-only"),
        pytest.param({"failed": 4}, (4, 0, 4), id="failed-only"),
        pytest.param(
            {"processed": 10, "applied": 0, "failed": 10}, (10, 0, 10), id="explicit-total-kept"
        ),
        pytest.param({"processed": 6}, (6, 6, 0), id="total-only"),
        pytest.param({}, (0, 0, 0), id="nothing"),
    ],
)
def test_the_total_and_the_split_agree(
    given: dict[str, int], expected: tuple[int, int, int]
) -> None:
    """``processed`` is ``applied + failed`` however a writer spells the pass;
    the derived shape survives a round trip unchanged."""
    outcome = DrainOutcome(**given)
    assert (outcome.processed, outcome.applied, outcome.failed) == expected
    dumped = outcome.model_dump(mode="json")
    assert (dumped["processed"], dumped["applied"], dumped["failed"]) == expected
    assert DrainOutcome.model_validate(dumped) == outcome


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(DrainInput, "limit", 0, id="limit-0"),
        pytest.param(DrainInput, "max_passes", 0, id="max-passes-0"),
        pytest.param(DrainInput, "locked_retries", -1, id="locked-retries-negative"),
        pytest.param(DrainInput, "locked_retry_seconds", -0.1, id="retry-seconds-negative"),
        pytest.param(DrainOutcome, "processed", -1, id="processed-negative"),
        pytest.param(DrainOutcome, "applied", -1, id="applied-negative"),
        pytest.param(DrainOutcome, "failed", -1, id="failed-negative"),
        pytest.param(DrainReport, "passes", -1, id="passes-negative"),
        pytest.param(DrainReport, "emails", -1, id="emails-negative"),
    ],
)
def test_counts_and_bounds_are_validated(
    model: type[VersionedModel], field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        model(**{field: value})


def test_drain_input_pinned_clock_round_trips_with_its_timezone() -> None:
    now = datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
    dumped = DrainInput(now=now).model_dump(mode="json")
    assert dumped["now"] == "2026-09-05T12:30:00Z"
    assert DrainInput.model_validate(dumped).now == now


def test_unknown_fields_from_a_newer_writer_survive_a_round_trip() -> None:
    raw = {"schema_version": "1.0.0", "processed": 3, "future_field": {"x": 1}}
    loaded = DrainOutcome.model_validate(raw)
    assert loaded.processed == 3
    assert loaded.model_dump(mode="json")["future_field"] == {"x": 1}


def test_an_older_stamp_is_upgraded_to_the_current_version_on_load() -> None:
    loaded = DrainReport.model_validate({"schema_version": "0.9.0", "passes": 1})
    assert loaded.schema_version == DrainReport.SCHEMA_VERSION
    assert json.loads(loaded.model_dump_json())["schema_version"] == DrainReport.SCHEMA_VERSION


def test_report_counters_can_be_incremented_in_place() -> None:
    """The workflow loop mutates one report across passes; assignment validation
    must accept the increments and still refuse a negative."""
    report = DrainReport()
    report.passes += 1
    report.processed += 40
    assert (report.passes, report.processed) == (1, 40)
    with pytest.raises(ValidationError):
        report.emails = -1


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(DrainInput(now=datetime(2026, 1, 2, tzinfo=UTC), limit=7), id="input"),
        pytest.param(DrainOutcome(processed=5, skipped_locked=True), id="outcome"),
        pytest.param(DrainOutcome(applied=5, failed=2), id="outcome-split"),
        pytest.param(DrainReport(passes=2, processed=9, emails=1, locked_retries=1), id="report"),
        pytest.param(
            ToolCallActivityInput(
                tool_name="read",
                session_id="s",
                call_id="c",
                org_id="o",
                input_ref="blob:1",
                idempotency_key="s:c",
            ),
            id="tool-call",
        ),
    ],
)
async def test_shapes_round_trip_through_temporals_pydantic_converter(
    value: VersionedModel,
) -> None:
    payloads = await pydantic_data_converter.encode([value])
    [decoded] = await pydantic_data_converter.decode(payloads, [type(value)])
    assert decoded == value
    assert isinstance(decoded, type(value))


def test_tool_call_input_requires_every_identity_field() -> None:
    with pytest.raises(ValidationError):
        ToolCallActivityInput(tool_name="read")  # type: ignore[call-arg]
