"""``alkera.chart``: the builder people and agents write charts with.

Every spec the builder emits is checked against the real profile copy it
ships, so a builder change that produced an inadmissible spec turns these red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import alkera
import pytest
from alkera.chart import Chart, ChartError
from alkera.chart._builder import DEFAULT_MAX_ROWS
from alkera.chart._profile import CHART_MIME, MAX_INLINE_ROWS, PROFILE

CORPUS = Path(__file__).resolve().parents[4] / "packages/api-core/tests/fixtures/charts/profile"

ROWS = [
    {"day": "2026-01-01", "region": "north", "units": 12, "price": 4.5},
    {"day": "2026-01-02", "region": "north", "units": 18, "price": 4.1},
    {"day": "2026-01-01", "region": "south", "units": 22, "price": 3.9},
    {"day": "2026-01-02", "region": "south", "units": 17, "price": 4.4},
]


def _data(spec: dict[str, Any]) -> list[dict[str, Any]]:
    (rows,) = spec["datasets"].values()
    return rows


# The entry point ----------------------------------------------------------------


def test_alkera_chart_is_callable_and_carries_the_api() -> None:
    chart = alkera.chart(ROWS)
    assert isinstance(chart, Chart)
    assert alkera.chart.Chart is Chart
    assert alkera.chart.MIME == CHART_MIME
    assert set(alkera.chart.profile()["marks"]) == set(PROFILE.marks)


def test_a_vega_lite_dict_is_kept_as_written() -> None:
    spec = json.loads((CORPUS / "valid" / "line_multi_series.json").read_text())
    out = alkera.chart(spec).to_dict()
    assert {k: v for k, v in out.items() if k != "$schema"} == spec


def test_an_altair_chart_is_read_through_its_own_to_dict() -> None:
    spec = json.loads((CORPUS / "altair" / "brush_condition.json").read_text())
    fake = type(
        "Chart", (), {"__module__": "altair.vegalite.v6.api", "to_dict": lambda self: spec}
    )()
    assert alkera.chart(fake).to_dict() == spec


def test_a_dict_of_columns_named_like_spec_keys_is_still_data() -> None:
    chart = alkera.chart({"mark": [1, 2], "layer": [3, 4]}).point(x="mark", y="layer")
    assert _data(chart.to_dict()) == [{"mark": 1, "layer": 3}, {"mark": 2, "layer": 4}]


# Encodings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shorthand", "expected"),
    [
        pytest.param("units", {"field": "units", "type": "quantitative"}, id="inferred_number"),
        pytest.param("day", {"field": "day", "type": "temporal"}, id="inferred_iso_date"),
        pytest.param("region", {"field": "region", "type": "nominal"}, id="inferred_text"),
        pytest.param("units:O", {"field": "units", "type": "ordinal"}, id="type_letter"),
        pytest.param("units:n", {"field": "units", "type": "nominal"}, id="type_letter_lowercase"),
        pytest.param(
            "sum(units)",
            {"field": "units", "aggregate": "sum", "type": "quantitative"},
            id="aggregate",
        ),
        pytest.param("count()", {"aggregate": "count", "type": "quantitative"}, id="count"),
        pytest.param(
            "yearmonth(day)",
            {"field": "day", "timeUnit": "yearmonth", "type": "temporal"},
            id="time_unit",
        ),
        pytest.param(
            "month(day):O",
            {"field": "day", "timeUnit": "month", "type": "ordinal"},
            id="time_unit_typed",
        ),
    ],
)
def test_shorthand_reads_like_altair(shorthand: str, expected: dict[str, Any]) -> None:
    spec = alkera.chart(ROWS).bar(x=shorthand, y="count()").to_dict()
    assert spec["encoding"]["x"] == expected


def test_a_column_named_like_shorthand_is_read_as_that_column() -> None:
    chart = alkera.chart([{"count(x)": 3, "units:Q": 1}]).bar(x="units:Q", y="count(x)")
    encoding = chart.to_dict()["encoding"]
    assert encoding["x"] == {"field": "units:Q", "type": "quantitative"}
    assert encoding["y"] == {"field": "count(x)", "type": "quantitative"}


def test_an_unknown_shorthand_function_is_refused_by_name() -> None:
    with pytest.raises(ChartError, match=r"frobnicate\(\) is neither"):
        alkera.chart(ROWS).bar(x="frobnicate(units)")


def test_a_column_with_a_dot_reads_that_column_not_a_nested_one() -> None:
    chart = alkera.chart([{"p.value": 0.01, "n": 3}]).point(x="p.value", y="n")
    assert chart.to_dict()["encoding"]["x"]["field"] == "p\\.value"


def test_value_and_bin_helpers_build_their_channel_shapes() -> None:
    spec = (
        alkera.chart(ROWS)
        .bar(
            x=alkera.chart.bin("price", maxbins=5), y="count()", color=alkera.chart.value("#1c7064")
        )
        .to_dict()
    )
    assert spec["encoding"]["x"] == {
        "field": "price",
        "type": "quantitative",
        "bin": {"maxbins": 5},
    }
    assert spec["encoding"]["color"] == {"value": "#1c7064"}


def test_encode_with_none_removes_a_channel() -> None:
    chart = alkera.chart(ROWS).line(x="day", y="units", color="region").encode(color=None)
    assert "color" not in chart.to_dict()["encoding"]


def test_a_channel_value_of_the_wrong_kind_is_explained() -> None:
    with pytest.raises(ChartError, match="takes a column name"):
        alkera.chart(ROWS).line(x=42)


# Marks --------------------------------------------------------------------------


MARK_CALLS = {
    "line": lambda c: c.line(x="day", y="units"),
    "area": lambda c: c.area(x="day", y="units"),
    "bar": lambda c: c.bar(x="region", y="sum(units)"),
    "point": lambda c: c.point(x="price", y="units"),
    "circle": lambda c: c.circle(x="price", y="units"),
    "square": lambda c: c.square(x="price", y="units"),
    "tick": lambda c: c.tick(x="price", y="region"),
    "rect": lambda c: c.heatmap(x="day:O", y="region", color="units"),
    "rule": lambda c: c.rule(y="mean(units)"),
    "text": lambda c: c.text(x="price", y="units", text="region"),
    "arc": lambda c: c.pie(theta="sum(units)", color="region", donut=True),
    "boxplot": lambda c: c.boxplot(x="region", y="units"),
    "errorbar": lambda c: c.errorbar(x="region", y="units", extent="stderr"),
    "errorband": lambda c: c.errorband(x="day", y="units"),
}


def test_every_profile_mark_has_a_builder() -> None:
    assert set(MARK_CALLS) == set(PROFILE.marks)


@pytest.mark.parametrize("mark", sorted(MARK_CALLS))
def test_each_mark_builds_an_admitted_spec_with_that_mark(mark: str) -> None:
    spec = MARK_CALLS[mark](alkera.chart(ROWS)).to_dict()
    drawn = spec["mark"]["type"] if isinstance(spec["mark"], dict) else spec["mark"]
    assert drawn == mark
    assert _data(spec) == ROWS


def test_histogram_bins_and_counts() -> None:
    encoding = alkera.chart(ROWS).histogram("price", bins=12).to_dict()["encoding"]
    assert encoding["x"]["bin"] == {"maxbins": 12}
    assert encoding["y"] == {"aggregate": "count", "type": "quantitative"}


def test_style_keywords_become_mark_properties() -> None:
    spec = (
        alkera.chart(ROWS)
        .line(x="day", y="units", style={"point": True, "stroke_width": 3})
        .to_dict()
    )
    assert spec["mark"] == {"type": "line", "point": True, "strokeWidth": 3}


# Interaction --------------------------------------------------------------------


def test_tooltip_without_fields_turns_on_the_marks_tooltip() -> None:
    assert (
        alkera.chart(ROWS).point(x="price", y="units").tooltip().to_dict()["mark"]["tooltip"]
        is True
    )


def test_tooltip_with_fields_names_them() -> None:
    spec = alkera.chart(ROWS).point(x="price", y="units").tooltip("region", "units").to_dict()
    assert [t["field"] for t in spec["encoding"]["tooltip"]] == ["region", "units"]


def test_tooltip_before_a_mark_is_explained() -> None:
    with pytest.raises(ChartError, match="follows a mark"):
        alkera.chart(ROWS).tooltip()


@pytest.mark.parametrize(("axes", "encodings"), [("x", ["x"]), ("y", ["y"]), ("xy", ["x", "y"])])
def test_zoom_binds_an_interval_to_the_scales(axes: str, encodings: list[str]) -> None:
    (param,) = alkera.chart(ROWS).point(x="price", y="units").zoom(axes).to_dict()["params"]
    assert param == {
        "name": "zoom",
        "select": {"type": "interval", "encodings": encodings},
        "bind": "scales",
    }


@pytest.mark.parametrize("axes", ["z", "", "xz"])
def test_zoom_refuses_an_axis_that_is_not_x_or_y(axes: str) -> None:
    with pytest.raises(ChartError, match="axes"):
        alkera.chart(ROWS).point(x="price", y="units").zoom(axes)


def test_brush_keeps_series_colors_inside_and_dims_outside() -> None:
    spec = alkera.chart(ROWS).point(x="price", y="units", color="region").brush().to_dict()
    assert spec["encoding"]["color"] == {
        "condition": {"param": "brush", "field": "region", "type": "nominal"},
        "value": "#9e9b94",
    }


def test_brush_on_one_series_dims_by_opacity() -> None:
    spec = alkera.chart(ROWS).point(x="price", y="units").brush().to_dict()
    assert spec["encoding"]["opacity"]["condition"] == {"param": "brush", "value": 1}
    assert "color" not in spec["encoding"]


def test_a_brush_in_one_view_filters_its_sibling() -> None:
    base = alkera.chart(ROWS)
    spec = (
        base.area(x="day", y="units").brush()
        & base.bar(x="region", y="sum(units)").filter_selection()
    ).to_dict()
    assert spec["vconcat"][1]["transform"] == [{"filter": {"param": "brush"}}]


def test_highlight_legend_binds_a_point_selection_to_the_legend() -> None:
    spec = alkera.chart(ROWS).line(x="day", y="units", color="region").highlight_legend().to_dict()
    assert spec["params"] == [
        {"name": "series", "select": {"type": "point", "fields": ["region"]}, "bind": "legend"}
    ]


def test_highlight_legend_without_a_color_column_is_explained() -> None:
    with pytest.raises(ChartError, match="color channel"):
        alkera.chart(ROWS).line(x="day", y="units").highlight_legend()


# Transforms ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "predicate"),
    [
        pytest.param({"gt": 15}, {"field": "units", "gt": 15}, id="one_term"),
        pytest.param(
            {"one_of": [12, 22]}, {"field": "units", "oneOf": [12, 22]}, id="snake_case_op"
        ),
        pytest.param(
            {"gte": 12, "lt": 20},
            {"and": [{"field": "units", "gte": 12}, {"field": "units", "lt": 20}]},
            id="terms_combine_with_and",
        ),
    ],
)
def test_filter_builds_a_structured_predicate(
    kwargs: dict[str, Any], predicate: dict[str, Any]
) -> None:
    spec = alkera.chart(ROWS).point(x="price", y="units").filter("units", **kwargs).to_dict()
    assert spec["transform"] == [{"filter": predicate}]


def test_filter_without_a_condition_is_explained() -> None:
    with pytest.raises(ChartError, match="needs a condition"):
        alkera.chart(ROWS).filter("units")


def test_a_regression_layer_over_points() -> None:
    base = alkera.chart(ROWS)
    spec = (
        base.point(x="price", y="units")
        + base.line(x="price", y="units").regression("units", on="price")
    ).to_dict()
    assert spec["layer"][1]["transform"] == [
        {"regression": "units", "on": "price", "method": "linear"}
    ]


def test_density_creates_the_fields_the_chart_then_reads() -> None:
    spec = alkera.chart(ROWS).density("price").area(x="value:Q", y="density:Q").to_dict()
    assert spec["transform"] == [{"density": "price", "as": ["value", "density"]}]


# Composition --------------------------------------------------------------------


@pytest.mark.parametrize(("op", "key"), [("+", "layer"), ("|", "hconcat"), ("&", "vconcat")])
def test_operators_compose_and_flatten(op: str, key: str) -> None:
    base = alkera.chart(ROWS)
    a, b, c = (
        base.point(x="price", y="units"),
        base.line(x="price", y="units"),
        base.rule(y="mean(units)"),
    )
    composed = {"+": lambda: a + b + c, "|": lambda: a | b | c, "&": lambda: a & b & c}[op]()
    spec = composed.to_dict()
    assert len(spec[key]) == 3
    assert len(spec["datasets"]) == 1, "the same table travels once"


def test_composing_with_something_that_is_not_a_chart_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        alkera.chart(ROWS).point(x="price", y="units") + 1


def test_facet_wraps_the_chart_and_keeps_its_data_outside() -> None:
    spec = alkera.chart(ROWS).bar(x="day:O", y="units").facet(column="region", columns=2).to_dict()
    assert spec["facet"] == {"column": {"field": "region", "type": "nominal"}}
    assert spec["spec"]["mark"] == "bar"
    assert spec["columns"] == 2
    assert "data" in spec


def test_facet_needs_a_row_or_a_column() -> None:
    with pytest.raises(ChartError, match="row= or column="):
        alkera.chart(ROWS).bar(x="day", y="units").facet()


def test_a_builder_call_never_changes_the_chart_it_was_called_on() -> None:
    base = alkera.chart(ROWS).line(x="day", y="units")
    before = base.to_dict()
    base.title("Units").zoom().encode(color="region").size(width=300)
    assert base.to_dict() == before


# Sampling -----------------------------------------------------------------------


def _many(n: int) -> list[dict[str, Any]]:
    return [{"i": i, "v": i % 7} for i in range(n)]


def test_a_chart_over_the_row_limit_is_sampled_systematically_and_says_so() -> None:
    spec = alkera.chart(_many(DEFAULT_MAX_ROWS * 3)).line(x="i", y="v").to_dict()
    rows = _data(spec)
    assert len(rows) == DEFAULT_MAX_ROWS
    assert rows[0]["i"] == 0
    assert rows[-1]["i"] == DEFAULT_MAX_ROWS * 3 - 1
    assert [r["i"] for r in rows] == sorted(r["i"] for r in rows)
    assert spec["usermeta"] == {
        "alkera": {
            "sampled": {
                "rows": DEFAULT_MAX_ROWS,
                "of": DEFAULT_MAX_ROWS * 3,
                "method": "systematic",
            }
        }
    }


def test_sampling_is_deterministic() -> None:
    chart = alkera.chart(_many(12_345)).point(x="i", y="v")
    assert chart.to_dict() == chart.to_dict()


def test_a_chart_under_the_limit_is_not_sampled() -> None:
    spec = alkera.chart(_many(DEFAULT_MAX_ROWS)).line(x="i", y="v").to_dict()
    assert len(_data(spec)) == DEFAULT_MAX_ROWS
    assert "usermeta" not in spec


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda c: c.bar(x="v:O", y="sum(i)"), id="aggregate_channel"),
        pytest.param(lambda c: c.histogram("i"), id="bin"),
        pytest.param(lambda c: c.boxplot(x="v:O", y="i"), id="composite_mark"),
        pytest.param(lambda c: c.line(x="i", y="v").regression("v", on="i"), id="regression"),
        pytest.param(lambda c: c.density("i").area(x="value:Q", y="density:Q"), id="density"),
        pytest.param(lambda c: c.histogram("i").facet(column="v:N"), id="faceted_summary"),
    ],
)
def test_a_summarizing_chart_over_the_limit_is_refused_not_sampled(build: Any) -> None:
    chart = build(alkera.chart(_many(DEFAULT_MAX_ROWS + 1)))
    with pytest.raises(ChartError, match="a statistic over a sample"):
        chart.to_dict()


def test_max_rows_raises_the_limit_so_a_summary_stays_exact() -> None:
    spec = (
        alkera.chart(_many(DEFAULT_MAX_ROWS + 1))
        .max_rows(DEFAULT_MAX_ROWS + 1)
        .bar(x="v:O", y="sum(i)")
        .to_dict()
    )
    assert len(_data(spec)) == DEFAULT_MAX_ROWS + 1


@pytest.mark.parametrize("limit", [0, MAX_INLINE_ROWS + 1])
def test_max_rows_is_bounded_by_the_profile(limit: int) -> None:
    with pytest.raises(ChartError, match="max_rows"):
        alkera.chart(ROWS).max_rows(limit)


# Errors and display -------------------------------------------------------------


def test_a_typo_names_the_column_and_suggests_the_close_ones() -> None:
    with pytest.raises(ChartError) as refused:
        alkera.chart(ROWS).line(x="dya", y="units").to_dict()
    assert refused.value.path == "encoding.x.field"
    assert "'dya' is not a column" in str(refused.value)
    assert "did you mean 'day'" in str(refused.value)


def test_a_typo_in_a_layer_is_explained_from_that_layers_columns() -> None:
    base = alkera.chart(ROWS)
    with pytest.raises(ChartError, match="did you mean 'price'"):
        (base.point(x="price", y="units") + base.line(x="pirce", y="units")).to_dict()


def test_an_inadmissible_raw_spec_is_refused_with_the_profile_message() -> None:
    with pytest.raises(ChartError) as refused:
        alkera.chart({"mark": "point", "data": {"url": "https://evil.example/x.json"}}).to_dict()
    assert refused.value.path == "data"


def test_the_notebook_mime_bundle_carries_the_spec_under_the_alkera_type() -> None:
    chart = alkera.chart(ROWS).line(x="day", y="units")
    bundle = chart._repr_mimebundle_()
    assert bundle[CHART_MIME] == chart.to_dict()
    assert bundle["application/vnd.vegalite.v6+json"] == chart.to_dict()
    assert bundle["text/plain"] == "<alkera chart: line, 4 rows>"
    mime, payload = chart._mime_()
    assert (mime, json.loads(payload)) == (CHART_MIME, chart.to_dict())


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"values": "region,units\nnorth,1", "format": {"type": "csv"}}, id="csv"),
        pytest.param({"url": "sales.tsv"}, id="tsv_url"),
    ],
)
def test_a_raw_spec_with_csv_data_is_an_error_naming_the_conversion(
    data: dict[str, Any],
) -> None:
    chart = alkera.chart({"data": data, "mark": "bar"})
    with pytest.raises(ChartError, match=r"alkera\.chart reads CSV") as refused:
        chart.to_dict()
    assert refused.value.path == "data"
