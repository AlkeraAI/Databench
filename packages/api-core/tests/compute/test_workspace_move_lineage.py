"""The shapes a workspace move writes into workflow history load with the
current reader.

A move started before a deploy replays its step requests and outcomes on the
new code, so every fixture ever written under
``packages/api-core/tests/fixtures/workspace_move/v*/`` is loaded here, and the day one fails is
the day a running move would fail to replay. The fix is a ``MIGRATIONS``
entry, never an edit to the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.compute.workspace_move import StalledMoves, StepOutcome, StepRequest
from alkera_core.versioning import VersionedModel

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "workspace_move"
READERS: dict[str, type[VersionedModel]] = {
    "step_outcome": StepOutcome,
    "step_request": StepRequest,
    "stalled_moves": StalledMoves,
}


def _fixtures() -> list[object]:
    found = []
    for version in sorted(ROOT.iterdir()):
        for path in sorted(version.glob("*.json")):
            kind = next(k for k in READERS if path.stem.startswith(k))
            found.append(pytest.param(path, READERS[kind], id=f"{version.name}/{path.stem}"))
    return found


@pytest.mark.parametrize(("path", "reader"), _fixtures())
def test_every_fixture_loads_with_the_current_reader(
    path: Path, reader: type[VersionedModel]
) -> None:
    raw = json.loads(path.read_text())
    model = reader.model_validate(raw)
    assert model.schema_version == reader.SCHEMA_VERSION
    # Nothing the writer put down is lost on the way back.
    dumped = model.model_dump(mode="json")
    assert {k: dumped[k] for k in raw if k != "schema_version"} == {
        k: v for k, v in raw.items() if k != "schema_version"
    }


def test_every_shape_has_fixtures() -> None:
    kinds = {
        path.stem.split("_", 2)[0] + "_" + path.stem.split("_", 2)[1]
        for path in ROOT.glob("v*/*.json")
    }
    assert kinds == set(READERS)


def test_a_field_from_a_newer_writer_survives_the_round_trip() -> None:
    outcome = StepOutcome.model_validate({"state": "waking", "progress_words": "Waking workspace"})
    assert outcome.model_dump(mode="json")["progress_words"] == "Waking workspace"
