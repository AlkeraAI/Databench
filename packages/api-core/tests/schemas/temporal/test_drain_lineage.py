"""Backward-compat lineage net for the shapes that cross Temporal history.

Auto-discovers EVERY historical fixture under ``packages/api-core/tests/fixtures/temporal/v*/`` and
loads it with the CURRENT reader. A workflow started before a deploy replays its
inputs and results on the new code, so the day this fails is the day a running
workflow would fail to replay -- the fix is a ``MIGRATIONS`` entry, never an
edit to the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.temporal import (
    DrainInput,
    DrainOutcome,
    DrainReport,
    SweepInput,
    ToolCallActivityInput,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "temporal"


def _discover(subdir: str) -> list[object]:
    out: list[object] = []
    for version_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not version_dir.is_dir() or not version_dir.name.startswith("v"):
            continue
        category_dir = version_dir / subdir
        if not category_dir.is_dir():
            continue
        for fixture in sorted(category_dir.glob("*.json")):
            out.append(pytest.param(fixture, id=f"{version_dir.name}/{subdir}/{fixture.stem}"))
    return out


@pytest.mark.parametrize("fixture_path", _discover("drain_input"))
def test_drain_input_fixture_loads(fixture_path: Path) -> None:
    model = DrainInput.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == DrainInput.SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", _discover("drain_outcome"))
def test_drain_outcome_fixture_loads(fixture_path: Path) -> None:
    model = DrainOutcome.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == DrainOutcome.SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", _discover("drain_report"))
def test_drain_report_fixture_loads(fixture_path: Path) -> None:
    model = DrainReport.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == DrainReport.SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", _discover("tool_call_activity_input"))
def test_tool_call_activity_input_fixture_loads(fixture_path: Path) -> None:
    model = ToolCallActivityInput.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == ToolCallActivityInput.SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", _discover("sweep_input"))
def test_sweep_input_fixture_loads(fixture_path: Path) -> None:
    model = SweepInput.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == SweepInput.SCHEMA_VERSION


def test_corpus_is_non_empty() -> None:
    """Guards against a vacuously-passing empty corpus (deleted/moved fixtures)."""
    assert _discover("drain_input")
    assert _discover("drain_outcome")
    assert _discover("drain_report")
    assert _discover("tool_call_activity_input")
    assert _discover("sweep_input")


def test_current_writer_output_is_in_the_corpus() -> None:
    """The generator must have been run for the CURRENT version: a bumped
    SCHEMA_VERSION with no matching fixture directory is a missing regression net."""
    for model in (DrainInput, DrainOutcome, DrainReport, SweepInput, ToolCallActivityInput):
        assert (FIXTURE_ROOT / f"v{model.SCHEMA_VERSION.replace('.', '_')}").is_dir(), model
