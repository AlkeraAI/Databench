"""The profile's mechanisms: policies, ceilings, binding, registration.

The corpus (``test_profile_corpus.py``) pins what is admitted and refused as a
whole; these pin each mechanism behind that, so removing one turns a case red.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
from alkera_core.charts import (
    BOUND,
    HOSTED,
    INLINE,
    MAX_INLINE_ROWS,
    PROFILE,
    ChartSpecError,
    default_profile,
    escape_field,
    field_root,
    validate,
)
from alkera_core.charts.profile import (
    MAX_CELL_CHARS,
    MAX_LAYERS,
    MAX_ROW_FIELDS,
    MAX_VIEWS,
    Num,
    Obj,
    Output,
    Transform,
)

ROWS = [{"a": 1, "b": 2.5, "c": "x"}]


def _point(**extra: Any) -> dict[str, Any]:
    return {
        "data": {"values": copy.deepcopy(ROWS)},
        "mark": "point",
        "encoding": {"x": {"field": "a", "type": "quantitative"}},
        **extra,
    }


# Data policies --------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "admitted"),
    [
        pytest.param(INLINE, False, id="inline_needs_the_dataset"),
        pytest.param(HOSTED, True, id="hosted_resolves_it_from_the_host"),
    ],
)
def test_a_named_table_outside_the_spec_is_a_host_table_only_when_hosted(
    policy: Any, admitted: bool
) -> None:
    spec = {"data": {"name": "result_rows"}, "mark": "point"}
    if admitted:
        assert validate(spec, policy=policy).data_refs == [("data.name", "result_rows")]
    else:
        with pytest.raises(ChartSpecError) as refused:
            validate(spec, policy=policy)
        assert refused.value.path == "data.name"


def test_a_host_table_skips_the_inline_binding_check_because_its_columns_are_unknown() -> None:
    spec = {
        "datasets": {"local": ROWS},
        "hconcat": [
            {
                "data": {"name": "local"},
                "mark": "point",
                "encoding": {"x": {"field": "a", "type": "quantitative"}},
            },
            {
                "data": {"name": "remote"},
                "mark": "point",
                "encoding": {"x": {"field": "z", "type": "quantitative"}},
            },
        ],
    }
    assert validate(spec, policy=HOSTED).rows == 1
    with pytest.raises(ChartSpecError):
        validate(spec, policy=INLINE)


def test_a_bound_chart_checks_binding_against_the_columns_the_caller_names() -> None:
    spec = {"mark": "point", "encoding": {"x": {"field": "a", "type": "quantitative"}}}
    assert validate(spec, policy=BOUND, columns=["a"]).fields == [("encoding.x.field", "a")]
    with pytest.raises(ChartSpecError) as refused:
        validate(spec, policy=BOUND, columns=["b"])
    assert refused.value.path == "encoding.x.field"


def test_a_bound_chart_without_columns_is_not_binding_checked() -> None:
    spec = {"mark": "point", "encoding": {"x": {"field": "anything", "type": "quantitative"}}}
    assert validate(spec, policy=BOUND).persisted()["mark"] == "point"


# Ceilings -------------------------------------------------------------------


CSV_MESSAGE = "holds CSV or TSV text, which charts do not parse"


@pytest.mark.parametrize(
    ("spec", "path"),
    [
        pytest.param(
            {"data": {"values": "a,b\n1,2", "format": {"type": "csv"}}}, "data", id="csv_format"
        ),
        pytest.param(
            {"data": {"values": "a\tb\n1\t2", "format": {"type": "tsv"}}}, "data", id="tsv_format"
        ),
        pytest.param(
            {"data": {"values": "a|b", "format": {"type": "dsv", "delimiter": "|"}}},
            "data",
            id="dsv_format",
        ),
        pytest.param(
            {"data": {"values": ROWS, "format": {"type": "CSV"}}}, "data", id="format_over_rows"
        ),
        pytest.param({"data": {"values": "a,b\n1,2"}}, "data", id="string_values"),
        pytest.param({"data": {"url": "sales.csv"}}, "data", id="csv_url"),
        pytest.param({"data": {"url": "https://x.test/a.TSV?v=1#top"}}, "data", id="tsv_url"),
        pytest.param(
            {"data": {"name": "t"}, "datasets": {"t": "a,b\n1,2"}}, "datasets.t", id="dataset"
        ),
    ],
)
def test_delimited_data_is_refused_with_its_own_reason(spec: dict[str, Any], path: str) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({**spec, "mark": "point"})
    assert CSV_MESSAGE in str(refused.value)
    assert "alkera.chart reads CSV" in str(refused.value)
    assert refused.value.path == path


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"url": "data.json"}, id="json_url"),
        pytest.param({"values": ROWS, "format": {"type": "json"}}, id="json_format"),
    ],
)
def test_other_unsupported_data_keeps_the_generic_refusal(data: dict[str, Any]) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({"data": data, "mark": "point"})
    assert CSV_MESSAGE not in str(refused.value)
    assert "unsupported keys" in str(refused.value)


def test_inline_rows_are_counted_across_every_data_block() -> None:
    half = MAX_INLINE_ROWS // 2 + 1
    spec = {
        "datasets": {"one": [{"a": i} for i in range(half)]},
        "data": {"values": [{"a": i} for i in range(half)]},
        "mark": "point",
    }
    with pytest.raises(ChartSpecError, match="inline rows") as refused:
        validate(spec)
    assert refused.value.path == "datasets.one"


def test_exactly_the_row_ceiling_is_admitted() -> None:
    spec = {"data": {"values": [{"a": i} for i in range(MAX_INLINE_ROWS)]}, "mark": "point"}
    assert validate(spec).rows == MAX_INLINE_ROWS


@pytest.mark.parametrize(
    ("row", "path"),
    [
        pytest.param({"a": "x" * (MAX_CELL_CHARS + 1)}, "data.values[0]", id="cell_too_long"),
        pytest.param(
            {f"f{i}": i for i in range(MAX_ROW_FIELDS + 1)}, "data.values[0]", id="too_many_fields"
        ),
        pytest.param({"a": float("nan")}, "data.values[0]", id="nan"),
        pytest.param({"a": float("inf")}, "data.values[0]", id="infinity"),
        pytest.param({"a": [1, 2]}, "data.values[0]", id="list_cell"),
    ],
)
def test_a_row_outside_the_cell_rules_is_refused(row: dict[str, Any], path: str) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({"data": {"values": [row]}, "mark": "point"})
    assert refused.value.path == path


@pytest.mark.parametrize(
    "cell",
    [pytest.param(None, id="null"), pytest.param(True, id="bool"), pytest.param(-3, id="int")],
)
def test_a_scalar_cell_is_admitted(cell: object) -> None:
    assert validate({"data": {"values": [{"a": cell}]}, "mark": "point"}).rows == 1


def test_layers_beyond_the_ceiling_are_refused() -> None:
    spec = {"data": {"values": ROWS}, "layer": [{"mark": "point"}] * (MAX_LAYERS + 1)}
    with pytest.raises(ChartSpecError) as refused:
        validate(spec)
    assert refused.value.path == "layer"


def test_views_beyond_the_ceiling_are_refused() -> None:
    spec = {"data": {"values": ROWS}, "hconcat": [{"mark": "point"}] * MAX_VIEWS}
    with pytest.raises(ChartSpecError, match="views"):
        validate(spec)


def test_nesting_beyond_the_ceiling_is_refused() -> None:
    spec: dict[str, Any] = {"mark": "point"}
    for _ in range(5):
        spec = {"hconcat": [spec]}
    with pytest.raises(ChartSpecError, match="deeper"):
        validate(spec)


# Structure ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("child", "path"),
    [
        pytest.param({"hconcat": [{"mark": "point"}]}, "layer[0]", id="concat_in_layer"),
        pytest.param({"mark": "point", "layer": []}, "layer[0]", id="two_kinds"),
        pytest.param({"encoding": {}}, "layer[0]", id="no_kind"),
    ],
)
def test_a_layer_child_is_a_mark_or_a_layer(child: dict[str, Any], path: str) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({"data": {"values": ROWS}, "layer": [child]})
    assert refused.value.path == path


def test_a_facet_must_name_its_inner_spec() -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({"data": {"values": ROWS}, "facet": {"column": {"field": "c", "type": "nominal"}}})
    assert refused.value.path == "spec"


def test_a_selection_name_may_not_repeat() -> None:
    params = [{"name": "p", "select": "point"}, {"name": "p", "select": "interval"}]
    with pytest.raises(ChartSpecError) as refused:
        validate(_point(params=params))
    assert refused.value.path == "params[1].name"


def test_a_predicate_nests_and_every_leaf_is_checked() -> None:
    deep = {
        "filter": {
            "and": [{"or": [{"not": {"field": "a", "gt": 0}}, {"field": "c", "oneOf": ["x"]}]}]
        }
    }
    assert validate(_point(transform=[deep])).fields[0] == (
        "transform[0].filter.and[0].or[0].not.field",
        "a",
    )
    bad = {"filter": {"and": [{"or": [{"not": {"field": "a", "gt": "0"}}]}]}}
    with pytest.raises(ChartSpecError):
        validate(_point(transform=[bad]))


def test_refusal_order_does_not_depend_on_key_order() -> None:
    spec = {
        "encoding": {"href": {"field": "a", "type": "nominal"}},
        "mark": "image",
        "data": {"values": ROWS},
    }
    reordered = {"mark": "image", "data": {"values": ROWS}, "encoding": spec["encoding"]}
    with pytest.raises(ChartSpecError) as one:
        validate(spec)
    with pytest.raises(ChartSpecError) as two:
        validate(reordered)
    assert one.value.path == two.value.path


@pytest.mark.parametrize(
    "color",
    [
        pytest.param("#1c7064", id="hex6"),
        pytest.param("#abc", id="hex3"),
        pytest.param("steelblue", id="named"),
        pytest.param("rgb(10, 20, 30)", id="rgb"),
        pytest.param("hsla(120, 50%, 50%, 0.5)", id="hsla"),
    ],
)
def test_a_css_color_literal_is_admitted(color: str) -> None:
    assert validate(_point(mark={"type": "point", "color": color})).marks == {"point"}


@pytest.mark.parametrize(
    "color",
    [
        pytest.param("url(#g)", id="paint_server"),
        pytest.param("var(--x)", id="css_var"),
        pytest.param("rgb(calc(1))", id="nested_function"),
        pytest.param("red;stroke:url(x)", id="style_injection"),
    ],
)
def test_anything_else_in_a_color_slot_is_refused(color: str) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate(_point(mark={"type": "point", "color": color}))
    assert refused.value.path == "mark.color"


@pytest.mark.parametrize(
    "fmt",
    [
        pytest.param("{a}", id="braces"),
        pytest.param("<b>", id="markup"),
        pytest.param("\\x", id="backslash"),
    ],
)
def test_a_format_is_a_bounded_pattern_not_markup(fmt: str) -> None:
    spec = _point()
    spec["encoding"]["x"]["format"] = fmt
    with pytest.raises(ChartSpecError) as refused:
        validate(spec)
    assert refused.value.path == "encoding.x.format"


# Fields ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "root"),
    [
        pytest.param("price", "price", id="plain"),
        pytest.param("a.b", "a", id="nested"),
        pytest.param("a[0]", "a", id="indexed"),
        pytest.param("p\\.value", "p.value", id="escaped_dot"),
        pytest.param("x\\[1\\]", "x[1]", id="escaped_brackets"),
    ],
)
def test_field_root_reads_vega_lite_access_paths(field: str, root: str) -> None:
    assert field_root(field) == root


@pytest.mark.parametrize("column", ["p.value", "x[1]", "back\\slash", "plain"])
def test_escape_field_round_trips_through_field_root(column: str) -> None:
    assert field_root(escape_field(column)) == column


def test_a_transform_output_binds_and_its_input_does_not() -> None:
    spec = _point(
        transform=[{"aggregate": [{"op": "mean", "field": "b", "as": "avg"}], "groupby": ["c"]}],
        encoding={
            "x": {"field": "avg", "type": "quantitative"},
            "y": {"field": "c", "type": "nominal"},
        },
    )
    chart = validate(spec)
    assert "avg" in chart.produced
    assert chart.unbound_fields(["b", "c"]) == []
    assert chart.unbound_fields(["c"]) == ["transform[0].aggregate[0].field"]


@pytest.mark.parametrize(
    ("transform", "outputs"),
    [
        pytest.param({"fold": ["a", "b"]}, {"key", "value"}, id="fold_defaults"),
        pytest.param({"density": "b"}, {"value", "density"}, id="density_defaults"),
        pytest.param({"quantile": "b"}, {"prob", "value"}, id="quantile_defaults"),
        pytest.param({"bin": True, "field": "b", "as": "bb"}, {"bb", "bb_end"}, id="bin_with_end"),
        pytest.param({"regression": "b", "on": "a"}, {"a", "b"}, id="regression_keeps_names"),
    ],
)
def test_each_transform_declares_the_fields_it_creates(
    transform: dict[str, Any], outputs: set[str]
) -> None:
    assert outputs <= validate(_point(transform=[transform])).produced


# Registration ---------------------------------------------------------------


def test_a_registered_transform_is_admitted_by_that_profile_only() -> None:
    extended = default_profile()
    extended.register_transform(
        Transform(
            "scale_by",
            Obj({"scale_by": Num(0, 10), "as": Output()}, required=("scale_by", "as")),
            lambda t: [t["as"]],
            "multiply every measure",
        )
    )
    spec = _point(
        transform=[{"scale_by": 2, "as": "scaled"}],
        encoding={"x": {"field": "scaled", "type": "quantitative"}},
    )
    assert validate(spec, profile=extended).produced == {"scaled"}
    with pytest.raises(ChartSpecError):
        validate(spec)


def test_a_registered_mark_is_admitted_with_its_own_properties() -> None:
    extended = default_profile()
    extended.register_mark("trail", {"interpolate": PROFILE.marks["line"]["interpolate"]})
    spec = _point(mark={"type": "trail", "interpolate": "monotone"})
    assert validate(spec, profile=extended).marks == {"trail"}
    with pytest.raises(ChartSpecError):
        validate(spec)


def test_a_registration_after_rules_were_built_still_counts() -> None:
    extended = default_profile()
    extended.aggregates.add("exponential_mean")
    spec = _point()
    spec["encoding"]["x"]["aggregate"] = "exponential_mean"
    assert validate(spec, profile=extended).fields
    with pytest.raises(ChartSpecError):
        validate(spec)


def test_the_summary_lists_the_registered_vocabulary() -> None:
    summary = PROFILE.summary()
    assert set(summary["marks"]) == set(PROFILE.marks)
    assert "calculate" in summary["transforms"]
    assert summary["expressions"]["functions"] == sorted(PROFILE.expression_functions)
    assert "data" not in summary["expressions"]["functions"]
    assert summary["limits"]["inline_rows"] == MAX_INLINE_ROWS


# Portability ----------------------------------------------------------------


def test_the_profile_module_is_standard_library_only_and_parses_as_python_3_10() -> None:
    source = Path(__file__).resolve().parents[2] / "alkera_core" / "charts" / "profile.py"
    tree = ast.parse(source.read_text(), feature_version=(3, 10))
    imported = {
        (node.module or "").split(".")[0]
        if isinstance(node, ast.ImportFrom)
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {"__future__", "json", "math", "re", "collections", "typing"}
