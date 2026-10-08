"""The settings schema is the one owner of every notebook setting: the
engine's typed models admit exactly its values, the header reads and writes
every one, and each has a control or says why it has none."""

from __future__ import annotations

import typing
from typing import Any

import pytest
from alkera_notebook.engine.models import Settings, SettingsChange
from alkera_notebook.format.header import parse_settings, render_fence
from alkera_notebook.format.settings import NOTEBOOK_SETTINGS, export_schema


def _literal(model: type[Any], name: str) -> set[Any]:
    annotation = model.model_fields[name].annotation
    if typing.get_origin(annotation) is typing.Literal:
        return set(typing.get_args(annotation))
    found: set[Any] = set()
    for arm in typing.get_args(annotation):
        if typing.get_origin(arm) is typing.Literal:
            found |= set(typing.get_args(arm))
    return found


@pytest.mark.parametrize(
    "spec", [s for s in NOTEBOOK_SETTINGS if s.type == "enum"], ids=lambda s: s.name
)
def test_the_engine_models_admit_exactly_the_schema_s_values(spec: Any) -> None:
    wanted = {value for value, _ in spec.choices}
    assert _literal(Settings, spec.name) == wanted
    assert _literal(SettingsChange, spec.name) == wanted


def test_every_setting_is_in_the_engine_models() -> None:
    names = {s.name for s in NOTEBOOK_SETTINGS}
    assert names <= set(Settings.model_fields)
    assert names <= set(SettingsChange.model_fields)


def test_every_exported_setting_has_a_control_or_a_reason() -> None:
    schema = export_schema()
    specs = [*schema["notebook"], *(s for kind in schema["cells"].values() for s in kind)]
    for spec in specs:
        assert spec["control"] != "none" or spec.get("reason"), spec["name"]


@pytest.mark.parametrize(
    ("toml", "value", "kept"),
    [
        pytest.param("1000", 1000, True, id="in-range"),
        pytest.param("0", 0, False, id="below-one"),
        pytest.param("10000001", 10_000_001, False, id="above-the-most"),
        pytest.param("true", True, False, id="a-bool-is-not-a-number"),
        pytest.param('"1000"', "1000", False, id="text"),
    ],
)
def test_the_header_reads_and_writes_the_sql_row_limit(toml: str, value: Any, kept: bool) -> None:
    assert ("sql_row_limit = " in render_fence("1.0", {"sql_row_limit": value}, "")) == kept
    parsed = parse_settings(f'format = "1.0"\nsql_row_limit = {toml}\n', 1)
    assert ("sql_row_limit" in parsed.values) == kept
    assert bool(parsed.violations) != kept
