"""The backwards-compatibility net for every persisted Files shape.

Walks every fixture under ``packages/api-core/tests/fixtures/files/v*/`` — one parametrized case
per file, named for its path — resolves the ``$model`` key the generator wrote,
and loads it with TODAY's reader. A failure here means a writer that shipped
once can no longer be read: the fix is a ``MIGRATIONS`` entry from that version,
never an edit to the fixture.

Each case also pins the two properties the whole scheme rests on: the reader
re-serialises at the CURRENT ``SCHEMA_VERSION`` (so a stale stamp can never be
written back), and an unknown field from a newer writer survives the round trip
(``extra="allow"`` is never quietly narrowed to ``forbid``).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import pytest
from alkera_core.versioning import VersionedModel
from alkera_core.versioning.base import Migration
from pydantic import ValidationError

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "files"
GENERATOR_PATH = FIXTURE_ROOT / "generate.py"

#: The key the generator stamps on every fixture, naming the writing class.
MODEL_KEY = "$model"

#: What a newer writer might add that this reader has never heard of.
UNKNOWN_FIELD = "field_from_a_newer_writer"


def _load_generator() -> ModuleType:
    """Import the committed generator by path and publish it under its own name.

    The fixtures tree is deliberately not a package, so ``$model`` paths that
    point at a shape defined in the generator (the probe record) are only
    resolvable once the module is registered in ``sys.modules``.
    """
    spec = importlib.util.spec_from_file_location("alkera_files_fixture_generator", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.GENERATOR_MODULE == spec.name
    return module


GENERATOR = _load_generator()


def _resolve(dotted: str) -> type[VersionedModel]:
    module_name, _, qualname = dotted.rpartition(".")
    obj: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    assert issubclass(obj, VersionedModel), f"{dotted} is not a VersionedModel"
    return obj


def _discover() -> list[Any]:
    out: list[Any] = []
    for fixture in sorted(FIXTURE_ROOT.glob("v*/*.json")):
        out.append(pytest.param(fixture, id=str(fixture.relative_to(FIXTURE_ROOT))))
    return out


FIXTURES = _discover()


def test_the_corpus_is_not_empty() -> None:
    """A discovery bug that finds nothing would make every case below vacuous."""
    assert FIXTURES


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_a_historical_fixture_loads_with_todays_reader(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    model_type = _resolve(payload.pop(MODEL_KEY))
    model_type.model_validate(payload)


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_a_fixture_re_serialises_at_the_current_schema_version(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    model_type = _resolve(payload.pop(MODEL_KEY))
    dumped = model_type.model_validate(payload).model_dump(mode="json")
    assert dumped["schema_version"] == model_type.SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_an_unknown_field_from_a_newer_writer_survives_the_round_trip(
    fixture_path: Path,
) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    model_type = _resolve(payload.pop(MODEL_KEY))
    payload[UNKNOWN_FIELD] = {"added": "by a newer writer", "n": 7}
    dumped = model_type.model_validate(payload).model_dump(mode="json")
    assert dumped[UNKNOWN_FIELD] == {"added": "by a newer writer", "n": 7}


@pytest.mark.parametrize("fixture_path", FIXTURES)
def test_a_fixture_lives_in_the_directory_its_version_names(fixture_path: Path) -> None:
    """``v1_0_0/x.json`` must hold a payload stamped ``1.0.0`` — otherwise the
    corpus stops being a version-by-version history."""
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    payload.pop(MODEL_KEY)
    stamped = payload["schema_version"]
    assert fixture_path.parent.name == f"v{stamped.replace('.', '_')}"


def test_regenerating_reproduces_the_committed_bytes(tmp_path: Path) -> None:
    """The committed corpus is exactly what today's writer emits.

    If it is not, either the fixture was hand-edited or a shape changed without
    a version bump — both are the failure this net exists to catch.
    """
    written = GENERATOR.regenerate(tmp_path)
    assert written
    for produced in written:
        committed = FIXTURE_ROOT / produced.relative_to(tmp_path)
        assert committed.is_file(), f"{committed} is missing — run generate.py and commit it"
        assert produced.read_text() == committed.read_text()


def test_an_unmigrated_version_is_refused_rather_than_silently_read() -> None:
    """The negative twin: a payload from a version with no migration must fail.

    ``VersionedModel`` walks the ladder from the recorded version; with no entry
    for it the old field names never become the new ones, and validation refuses
    the payload instead of reading a half-understood record.
    """

    class _Renamed(VersionedModel):
        SCHEMA_VERSION: ClassVar[str] = "2.0.0"
        # Its own ladder: inheriting the base dict would let this test's entry
        # leak into every other model in the process.
        MIGRATIONS: ClassVar[dict[str, Migration]] = {}

        name: str

    stale = {"schema_version": "1.0.0", "label": "written before the rename"}
    with pytest.raises(ValidationError):
        _Renamed.model_validate(stale)

    # ... and with the migration registered, the very same payload loads.
    def _rename(data: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": "2.0.0", "name": data["label"]}

    _Renamed.MIGRATIONS["1.0.0"] = _rename
    try:
        assert _Renamed.model_validate(stale).name == "written before the rename"
    finally:
        del _Renamed.MIGRATIONS["1.0.0"]
