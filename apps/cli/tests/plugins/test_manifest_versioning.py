"""PluginManifest v2.0.0 — the removal-migration contract for the retired
``declared_capabilities.max_effect`` (the never-enforced per-plugin effect
ceiling): a 1.0.0 document migrates, re-emits the current shape, and the dead
key does not survive the round-trip. (The full historical corpus is exercised
by ``test_plugin_lineage.py``.)"""

from __future__ import annotations

import pytest
from alkera_cli.plugins.plugin_base import PluginManifest


def test_v1_0_0_manifest_with_max_effect_migrates_to_current() -> None:
    payload = {
        "schema_version": "1.0.0",
        "name": "snowflake",
        "version": "0.1.0",
        "surfaces": ["connection", "capability"],
        "declared_capabilities": {"max_effect": "destroy"},
        "description": "Snowflake warehouse connector",
    }
    manifest = PluginManifest.model_validate(payload)
    dumped = manifest.model_dump(mode="json")
    assert dumped["schema_version"] == PluginManifest.SCHEMA_VERSION == "2.0.0"
    assert "max_effect" not in dumped["declared_capabilities"]
    assert dumped["name"] == "snowflake"
    assert dumped["description"] == "Snowflake warehouse connector"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"schema_version": "1.0.0", "name": "duckdb_local"}, id="no-capabilities"),
        pytest.param(
            {"schema_version": "1.0.0", "name": "dbt", "declared_capabilities": {}},
            id="empty-capabilities",
        ),
    ],
)
def test_v1_0_0_manifest_without_max_effect_migrates_cleanly(payload: dict[str, object]) -> None:
    manifest = PluginManifest.model_validate(payload)
    assert manifest.model_dump(mode="json")["schema_version"] == "2.0.0"
