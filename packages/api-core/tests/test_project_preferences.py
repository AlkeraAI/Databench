"""ProjectPreferences — the per-project (team-shared) `.alkera/preferences.yml`
model. As a persisted `VersionedModel`, it must round-trip, preserve a newer
writer's unknown fields, and load every historical fixture with the current reader
(the lineage regression net)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.project_preferences import ProjectPreferences

_FIXTURES = Path(__file__).parent / "fixtures" / "project_preferences"


def test_defaults_inherit_org():
    """A zero-config project inherits the org sync setting (`None`)."""
    prefs = ProjectPreferences()
    assert prefs.kb_sync_enabled is None
    assert prefs.schema_version == "1.0.0"


@pytest.mark.parametrize("value", [True, False, None])
def test_round_trips_kb_sync_enabled(value: bool | None):
    prefs = ProjectPreferences(kb_sync_enabled=value)
    again = ProjectPreferences.model_validate(json.loads(prefs.model_dump_json()))
    assert again.kb_sync_enabled is value


def test_unknown_field_from_newer_writer_survives():
    """`extra="allow"` — a field a newer peer added rides through an older reader's
    round-trip instead of being dropped (forward compatibility)."""
    raw = {"schema_version": "1.0.0", "kb_sync_enabled": True, "future_flag": "keep me"}
    dumped = ProjectPreferences.model_validate(raw).model_dump()
    assert dumped["future_flag"] == "keep me"


@pytest.mark.parametrize("fixture", sorted(_FIXTURES.glob("v*.json")), ids=lambda p: p.stem)
def test_lineage_every_fixture_loads(fixture: Path):
    """Every historical writer's output still loads with today's reader, upgrading
    to the current schema version."""
    data = json.loads(fixture.read_text())
    prefs = ProjectPreferences.model_validate(data)
    assert prefs.schema_version == ProjectPreferences.SCHEMA_VERSION
