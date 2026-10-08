"""A saved result's chart: the chart profile under the bound data policy.

The profile's own vocabulary is pinned by ``packages/api-core/tests/charts``; this module pins
what is particular to a chart stored on a result: it never carries rows, it
binds to the result's column KEYS, and its refusals read as sentences that
name the offending key and never echo a value.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.schemas.objects import ChartSpecError, unbound_chart_fields, validate_chart_spec
from alkera_core.schemas.objects.chart import VEGA_LITE_V5_SCHEMA, VEGA_LITE_V6_SCHEMA


def _series(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "mark": "line",
        "encoding": {
            "x": {"field": "day", "type": "temporal"},
            "y": {"field": "orders", "type": "quantitative"},
        },
    }
    spec.update(overrides)
    return spec


def test_a_spec_without_a_schema_is_persisted_naming_vega_lite_six() -> None:
    persisted = validate_chart_spec(_series()).persisted()
    assert persisted["$schema"] == VEGA_LITE_V6_SCHEMA
    assert {k: v for k, v in persisted.items() if k != "$schema"} == _series()


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param(VEGA_LITE_V5_SCHEMA, id="v5_the_save_dialog"),
        pytest.param(VEGA_LITE_V6_SCHEMA, id="v6"),
        pytest.param(
            "https://vega.github.io/schema/vega-lite/v6.4.1.json", id="v6_as_altair_writes_it"
        ),
    ],
)
def test_a_named_vega_lite_schema_is_kept_as_written(schema: str) -> None:
    assert validate_chart_spec(_series(**{"$schema": schema})).persisted()["$schema"] == schema


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param("https://vega.github.io/schema/vega-lite/v4.json", id="v4"),
        pytest.param("https://vega.github.io/schema/vega/v6.json", id="vega_not_vega_lite"),
        pytest.param("http://vega.github.io/schema/vega-lite/v6.json", id="plain_http"),
        pytest.param(
            "https://vega.github.io.evil.example/schema/vega-lite/v6.json", id="lookalike_host"
        ),
    ],
)
def test_any_other_schema_url_is_refused(schema: str) -> None:
    with pytest.raises(ChartSpecError, match=r"\$schema"):
        validate_chart_spec(_series(**{"$schema": schema}))


def test_a_multi_series_chart_is_admitted_now_that_the_reader_draws_one() -> None:
    spec = _series()
    spec["encoding"]["color"] = {"field": "engine", "type": "nominal"}
    assert validate_chart_spec(spec).persisted()["encoding"]["color"]["field"] == "engine"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("data", {"url": "https://evil.example/rows.json"}, id="data_url"),
        pytest.param("data", {"values": [{"day": "2026-01-01"}]}, id="inline_rows"),
        pytest.param("datasets", {"rows": [{"day": "2026-01-01"}]}, id="datasets"),
    ],
)
def test_a_result_chart_never_carries_rows_of_its_own(key: str, value: object) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate_chart_spec(_series(**{key: value}))
    assert str(refused.value) == f"a chart spec carries unsupported keys: ['{key}']"
    assert "evil.example" not in str(refused.value)


def test_a_layer_child_may_not_bring_rows_either() -> None:
    spec = {"layer": [{**_series(), "data": {"values": [{"day": 1}]}}]}
    with pytest.raises(ChartSpecError) as refused:
        validate_chart_spec(spec)
    assert refused.value.path == "layer[0]"


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("mark: line", id="not_an_object"),
        pytest.param(["line"], id="a_list"),
        pytest.param({1: "line"}, id="non_string_key"),
    ],
)
def test_a_spec_that_is_not_an_object_of_string_keys_is_refused(spec: object) -> None:
    with pytest.raises(ChartSpecError):
        validate_chart_spec(spec)


@pytest.mark.parametrize(
    "mark",
    [pytest.param({"type": ["line"]}, id="unhashable_type"), pytest.param(["line"], id="list")],
)
def test_an_unhashable_mark_is_a_refusal_not_a_crash(mark: object) -> None:
    with pytest.raises(ChartSpecError, match="mark"):
        validate_chart_spec(_series(mark=mark))


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        pytest.param(["day", "orders"], [], id="both_bound"),
        pytest.param(["day"], ["encoding.y.field"], id="y_unbound"),
        pytest.param(["orders"], ["encoding.x.field"], id="x_unbound"),
        pytest.param([], ["encoding.x.field", "encoding.y.field"], id="nothing_bound"),
    ],
)
def test_unbound_chart_fields_names_each_unbound_path(keys: list[str], expected: list[str]) -> None:
    assert unbound_chart_fields(_series(), keys) == expected


def test_a_field_a_transform_creates_is_bound_without_being_a_column() -> None:
    spec = _series(
        transform=[{"window": [{"op": "sum", "field": "orders", "as": "running"}]}],
        encoding={
            "x": {"field": "day", "type": "temporal"},
            "y": {"field": "running", "type": "quantitative"},
        },
    )
    assert unbound_chart_fields(spec, ["day", "orders"]) == []
    assert unbound_chart_fields(spec, ["day"]) == ["transform[0].window[0].field"]


def test_an_escaped_dot_binds_to_the_column_literally_named_with_it() -> None:
    spec = _series(encoding={"x": {"field": "p\\.value", "type": "quantitative"}})
    assert unbound_chart_fields(spec, ["p.value"]) == []
    nested = _series(encoding={"x": {"field": "p.value", "type": "quantitative"}})
    assert unbound_chart_fields(nested, ["p.value"]) == ["encoding.x.field"]
    assert unbound_chart_fields(nested, ["p"]) == []


def test_a_pivot_makes_binding_uncheckable_rather_than_falsely_refused() -> None:
    spec = _series(
        transform=[{"pivot": "engine", "value": "orders", "groupby": ["day"]}],
        encoding={
            "x": {"field": "day", "type": "temporal"},
            "y": {"field": "duckdb", "type": "quantitative"},
        },
    )
    assert unbound_chart_fields(spec, ["day", "engine", "orders"]) == []


def test_unbound_chart_fields_on_a_spec_the_profile_refuses_is_empty() -> None:
    assert unbound_chart_fields({"mark": "image"}, ["day"]) == []
