"""Regenerate the chart profile corpus and the theme fixtures.

Run from the repo root::

    uv run python packages/api-core/tests/fixtures/charts/generate.py

The corpus is the contract between the three implementations of Alkera charts:

* ``profile/valid/*.json``: specs the profile admits, one or more per v1 mark,
  transform, composition and interaction. The Python validator must admit
  every one; the renderer (``packages/ui``) must draw every one.
* ``profile/altair/*.json``: specs as Altair 6.3 writes them (``to_dict()``),
  captured once by ``capture_altair.py`` and never edited. The profile must
  admit Altair's own spelling.
* ``profile/invalid/*.json``: ``{"spec", "path", "policy", "unsafe"}``. The
  validator must refuse each at ``path``; the ``unsafe`` ones would fetch or
  evaluate, and the renderer's own guard must refuse those too.
* ``profile/expressions/{admit,refuse}.json``: expression strings, each with a
  ``name``; ``admit`` cases also list the row ``fields`` they read. Both the
  profile's expression check and the renderer's guard must admit every
  ``admit`` case and refuse every ``refuse`` case.
  ``profile/expressions/allowlist.json`` is the profile's own allowlist
  (functions, constants, limits), which the guard's must equal.
* ``theme/{light,dark}.json``: ``alkera_core.charts.theme.scheme_config``,
  which the renderer's ``chartConfig`` must reproduce from the same tokens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alkera_core.charts.profile import (
    MAX_EXPRESSION_ARGS,
    MAX_EXPRESSION_CHARS,
    MAX_EXPRESSION_DEPTH,
    MAX_PAD_LENGTH,
    PROFILE,
)
from alkera_core.charts.theme import CHART_TOKENS, scheme_config

HERE = Path(__file__).resolve().parent

SALES = [
    {"day": "2026-01-01", "region": "north", "units": 12, "price": 4.5, "ok": True},
    {"day": "2026-01-02", "region": "north", "units": 18, "price": 4.1, "ok": True},
    {"day": "2026-01-03", "region": "north", "units": 9, "price": 5.2, "ok": False},
    {"day": "2026-01-01", "region": "south", "units": 22, "price": 3.9, "ok": True},
    {"day": "2026-01-02", "region": "south", "units": 17, "price": 4.4, "ok": None},
    {"day": "2026-01-03", "region": "south", "units": 25, "price": 3.7, "ok": True},
]
DATA = {"values": SALES}

X_DAY = {"field": "day", "type": "temporal"}
Y_UNITS = {"field": "units", "type": "quantitative"}
COLOR_REGION = {"field": "region", "type": "nominal"}


def unit(mark: Any, encoding: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"data": DATA, "mark": mark, "encoding": encoding, **extra}


VALID: dict[str, dict[str, Any]] = {
    "line_multi_series": unit(
        {"type": "line", "point": True, "tooltip": True},
        {"x": X_DAY, "y": Y_UNITS, "color": COLOR_REGION},
        title={"text": "Units by day", "subtitle": "Two regions"},
        description="Daily units sold in the north and south regions.",
    ),
    "area_stacked": unit(
        "area", {"x": X_DAY, "y": {**Y_UNITS, "stack": "zero"}, "color": COLOR_REGION}
    ),
    "bar_grouped": unit(
        "bar",
        {
            "x": {"field": "day", "type": "ordinal", "timeUnit": "yearmonthdate"},
            "xOffset": COLOR_REGION,
            "y": {"field": "units", "type": "quantitative", "aggregate": "sum"},
            "color": COLOR_REGION,
        },
    ),
    "bar_sorted_horizontal": unit(
        "bar",
        {
            "y": {"field": "region", "type": "nominal", "sort": "-x"},
            "x": {
                "field": "units",
                "type": "quantitative",
                "aggregate": "mean",
                "title": "Mean units",
            },
        },
    ),
    "histogram": unit(
        "bar",
        {
            "x": {"field": "price", "type": "quantitative", "bin": {"maxbins": 10}},
            "y": {"aggregate": "count", "type": "quantitative"},
        },
    ),
    "point_scatter_sized": unit(
        {"type": "point", "shape": "diamond"},
        {
            "x": {"field": "price", "type": "quantitative", "scale": {"zero": False}},
            "y": Y_UNITS,
            "size": {"field": "units", "type": "quantitative"},
            "shape": COLOR_REGION,
            "tooltip": [
                {"field": "region", "type": "nominal"},
                {"field": "price", "type": "quantitative", "format": ".2f"},
            ],
        },
    ),
    "circle": unit("circle", {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS}),
    "square": unit("square", {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS}),
    "tick_strip": unit(
        "tick", {"x": {"field": "price", "type": "quantitative"}, "y": COLOR_REGION}
    ),
    "rect_heatmap": unit(
        "rect",
        {
            "x": {"field": "day", "type": "ordinal", "timeUnit": "date"},
            "y": COLOR_REGION,
            "color": {"field": "units", "type": "quantitative", "scale": {"scheme": "blues"}},
        },
    ),
    "rule_threshold": {
        "data": DATA,
        "layer": [
            {"mark": "line", "encoding": {"x": X_DAY, "y": Y_UNITS, "color": COLOR_REGION}},
            {"mark": {"type": "rule", "strokeDash": [4, 4]}, "encoding": {"y": {"datum": 20}}},
        ],
    },
    "text_labels": unit(
        {"type": "text", "dy": -6, "fontSize": 11},
        {"x": X_DAY, "y": Y_UNITS, "text": {"field": "units", "type": "quantitative"}},
    ),
    "arc_donut": unit(
        {"type": "arc", "innerRadius": 40},
        {
            "theta": {"field": "units", "type": "quantitative", "aggregate": "sum"},
            "color": COLOR_REGION,
        },
    ),
    "boxplot": unit({"type": "boxplot", "extent": "min-max"}, {"x": COLOR_REGION, "y": Y_UNITS}),
    "errorbar_ci": {
        "data": DATA,
        "layer": [
            {
                "mark": {"type": "errorbar", "extent": "ci"},
                "encoding": {"x": COLOR_REGION, "y": Y_UNITS},
            },
            {
                "mark": "point",
                "encoding": {
                    "x": COLOR_REGION,
                    "y": {"field": "units", "type": "quantitative", "aggregate": "mean"},
                },
            },
        ],
    },
    "errorband_stdev": {
        "data": DATA,
        "layer": [
            {
                "mark": {"type": "errorband", "extent": "stdev"},
                "encoding": {"x": X_DAY, "y": Y_UNITS},
            },
            {
                "mark": "line",
                "encoding": {
                    "x": X_DAY,
                    "y": {"field": "units", "type": "quantitative", "aggregate": "mean"},
                },
            },
        ],
    },
    "regression_overlay": {
        "data": DATA,
        "layer": [
            {
                "mark": "point",
                "encoding": {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS},
            },
            {
                "mark": "line",
                "transform": [{"regression": "units", "on": "price", "method": "linear"}],
                "encoding": {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS},
            },
        ],
    },
    "loess_smooth": unit(
        "line",
        {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS},
        transform=[{"loess": "units", "on": "price", "bandwidth": 0.5}],
    ),
    "density": unit(
        "area",
        {
            "x": {"field": "value", "type": "quantitative"},
            "y": {"field": "density", "type": "quantitative"},
        },
        transform=[{"density": "price", "as": ["value", "density"]}],
    ),
    "window_cumulative": unit(
        "line",
        {"x": X_DAY, "y": {"field": "running", "type": "quantitative"}, "color": COLOR_REGION},
        transform=[
            {
                "window": [{"op": "sum", "field": "units", "as": "running"}],
                "groupby": ["region"],
                "sort": [{"field": "day"}],
            }
        ],
    ),
    "filter_fold_aggregate": unit(
        "bar",
        {
            "x": {"field": "measure", "type": "nominal"},
            "y": {"field": "total", "type": "quantitative"},
        },
        transform=[
            {
                "filter": {
                    "and": [{"field": "units", "gte": 10}, {"not": {"field": "ok", "equal": False}}]
                }
            },
            {"fold": ["units", "price"], "as": ["measure", "amount"]},
            {
                "aggregate": [{"op": "sum", "field": "amount", "as": "total"}],
                "groupby": ["measure"],
            },
        ],
    ),
    "quantile_qq": unit(
        "point",
        {
            "x": {"field": "prob", "type": "quantitative"},
            "y": {"field": "value", "type": "quantitative"},
        },
        transform=[{"quantile": "price", "step": 0.1}],
    ),
    "facet_columns": {
        "data": DATA,
        "facet": {"column": COLOR_REGION},
        "spec": {"mark": "bar", "encoding": {"x": X_DAY, "y": Y_UNITS}},
    },
    "zoom_pan": unit(
        "point",
        {"x": {"field": "price", "type": "quantitative"}, "y": Y_UNITS},
        params=[{"name": "zoom", "select": "interval", "bind": "scales"}],
    ),
    "brush_highlight": unit(
        "point",
        {
            "x": {"field": "price", "type": "quantitative"},
            "y": Y_UNITS,
            "color": {"condition": {"param": "brush", **COLOR_REGION}, "value": "grey"},
        },
        params=[{"name": "brush", "select": {"type": "interval", "encodings": ["x"]}}],
    ),
    "legend_toggle": unit(
        "line",
        {
            "x": X_DAY,
            "y": Y_UNITS,
            "color": COLOR_REGION,
            "opacity": {"condition": {"param": "series", "value": 1}, "value": 0.2},
        },
        params=[
            {"name": "series", "select": {"type": "point", "fields": ["region"]}, "bind": "legend"}
        ],
    ),
    "brush_filters_sibling": {
        "data": DATA,
        "vconcat": [
            {
                "name": "overview",
                "mark": "area",
                "encoding": {"x": X_DAY, "y": Y_UNITS},
            },
            {
                "mark": "bar",
                "transform": [{"filter": {"param": "window"}}],
                "encoding": {
                    "x": COLOR_REGION,
                    "y": {"field": "units", "type": "quantitative", "aggregate": "sum"},
                },
            },
        ],
        "params": [
            {
                "name": "window",
                "select": {"type": "interval", "encodings": ["x"]},
                "views": ["overview"],
            }
        ],
    },
    "expressions": unit(
        {"type": "point", "tooltip": True},
        {
            "x": X_DAY,
            "y": {"field": "revenue", "type": "quantitative"},
            "color": {
                "condition": {
                    "test": "datum.revenue > 70 && datum.region === 'south'",
                    "value": "#c0392b",
                },
                "value": "#7f8c8d",
            },
        },
        transform=[
            {"calculate": "round(datum.units * datum.price * 100) / 100", "as": "revenue"},
            {"filter": "isValid(datum.ok) && datum.units >= 10"},
            {"filter": {"and": [{"field": "units", "lt": 30}, "upper(datum.region) !== 'EAST'"]}},
        ],
    ),
    "named_dataset": {
        "datasets": {"sales": SALES},
        "data": {"name": "sales"},
        "mark": "bar",
        "encoding": {
            "x": COLOR_REGION,
            "y": {"field": "units", "type": "quantitative", "aggregate": "sum"},
        },
        "usermeta": {"alkera": {"sampled": {"rows": 6, "of": 600, "method": "systematic"}}},
    },
}

URL = "https://evil.example/rows.json"
ROWS = {"values": [{"a": 1}]}
INVALID: dict[str, dict[str, Any]] = {
    "remote_data": {
        "spec": {"data": {"url": URL}, "mark": "point"},
        "path": "data",
        "unsafe": True,
    },
    "remote_data_in_layer": {
        "spec": {"data": ROWS, "layer": [{"data": {"url": URL}, "mark": "point"}]},
        "path": "layer[0].data",
        "unsafe": True,
    },
    "image_mark": {
        "spec": {
            "data": ROWS,
            "mark": "image",
            "encoding": {"url": {"field": "a", "type": "nominal"}},
        },
        "path": "mark",
        "unsafe": True,
    },
    "href_channel": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {"href": {"field": "a", "type": "nominal"}},
        },
        "path": "encoding",
        "unsafe": True,
    },
    "calculate_constructor": {
        "spec": {
            "data": ROWS,
            "transform": [{"calculate": "datum.a.constructor", "as": "b"}],
            "mark": "point",
        },
        "path": "transform[0].calculate",
        "unsafe": True,
    },
    "calculate_data_function": {
        "spec": {
            "data": ROWS,
            "transform": [{"calculate": "data('elsewhere')", "as": "b"}],
            "mark": "point",
        },
        "path": "transform[0].calculate",
        "unsafe": True,
    },
    "calculate_not_a_string": {
        "spec": {
            "data": ROWS,
            "transform": [{"calculate": {"expr": "now()"}, "as": "b"}],
            "mark": "point",
        },
        "path": "transform[0].calculate",
        "unsafe": True,
    },
    "filter_expression_reads_a_signal": {
        "spec": {"data": ROWS, "transform": [{"filter": "width > 0"}], "mark": "point"},
        "path": "transform[0].filter",
        "unsafe": True,
    },
    "filter_expression_inside_and": {
        "spec": {
            "data": ROWS,
            "transform": [{"filter": {"and": [{"field": "a", "gt": 0}, "this.constructor"]}}],
            "mark": "point",
        },
        "path": "transform[0].filter.and[1]",
        "unsafe": True,
    },
    "expr_param": {
        "spec": {"data": ROWS, "mark": "point", "params": [{"name": "p", "expr": "now()"}]},
        "path": "params[0]",
        "unsafe": True,
    },
    "expr_value": {
        "spec": {"data": ROWS, "mark": {"type": "point", "size": {"expr": "width"}}},
        "path": "mark.size",
        "unsafe": True,
    },
    "label_expr": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {
                "x": {"field": "a", "type": "quantitative", "axis": {"labelExpr": "datum.label"}}
            },
        },
        "path": "encoding.x.axis",
        "unsafe": True,
    },
    "condition_test_escaped_proto": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {
                "color": {
                    "condition": {"test": "datum['\\x5f_proto__'] != null", "value": "red"},
                    "value": "blue",
                }
            },
        },
        "path": "encoding.color.condition.test",
        "unsafe": True,
    },
    "input_binding": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "params": [{"name": "p", "select": "point", "bind": {"input": "range"}}],
        },
        "path": "params[0].bind",
        "unsafe": True,
    },
    "paint_server_color": {
        "spec": {"data": ROWS, "mark": {"type": "point", "color": f"url({URL}#g)"}},
        "path": "mark.color",
        "unsafe": True,
    },
    "paint_server_channel_value": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {"color": {"value": f"url({URL}#g)"}},
        },
        "path": "encoding.color.value",
        "unsafe": True,
    },
    "paint_server_in_a_condition": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "params": [{"name": "p", "select": "point"}],
            "encoding": {
                "fill": {"condition": {"param": "p", "value": f"url({URL}#g)"}, "value": "red"}
            },
        },
        "path": "encoding.fill.condition.value",
        "unsafe": True,
    },
    "unscaled_paint_from_data": {
        "spec": {
            "data": {"values": [{"a": "url(https://evil.example/rows.json#g)"}]},
            "mark": "point",
            "encoding": {"color": {"field": "a", "type": "nominal", "scale": None}},
        },
        "path": "encoding.color.scale",
        "unsafe": True,
    },
    "foreign_schema": {
        "spec": {"$schema": "https://evil.example/schema.json", "data": ROWS, "mark": "point"},
        "path": "$schema",
        "unsafe": True,
    },
    "nested_cell": {
        "spec": {"data": {"values": [{"a": {"b": 1}}]}, "mark": "point"},
        "path": "data.values[0]",
        "unsafe": False,
    },
    "unknown_field": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {"x": {"field": "aa", "type": "quantitative"}},
        },
        "path": "encoding.x.field",
        "unsafe": False,
    },
    "field_without_type": {
        "spec": {"data": ROWS, "mark": "point", "encoding": {"x": {"field": "a"}}},
        "path": "encoding.x.type",
        "unsafe": False,
    },
    "undeclared_selection": {
        "spec": {
            "data": ROWS,
            "mark": "point",
            "encoding": {
                "color": {"condition": {"param": "nope", "value": "red"}, "value": "blue"}
            },
        },
        "path": "encoding.color.condition.param",
        "unsafe": False,
    },
    "missing_dataset": {
        "spec": {"data": {"name": "elsewhere"}, "mark": "point"},
        "path": "data.name",
        "unsafe": False,
    },
    "two_view_kinds": {
        "spec": {"data": ROWS, "mark": "point", "layer": [{"mark": "line"}]},
        "path": "",
        "unsafe": False,
    },
    "reserved_selection_name": {
        "spec": {"data": ROWS, "mark": "point", "params": [{"name": "width", "select": "point"}]},
        "path": "params[0].name",
        "unsafe": False,
    },
    "bound_chart_with_rows": {
        "spec": {"data": ROWS, "mark": "point"},
        "path": "",
        "policy": "bound",
        "unsafe": False,
    },
}


# Expressions -----------------------------------------------------------------

DEEPEST = MAX_EXPRESSION_DEPTH - 1  # parentheses inside the outermost level


def _long(chars: int) -> str:
    """A well-formed expression exactly ``chars`` characters long."""
    head = "datum.a"
    body = head + " + 1" * ((chars - len(head)) // 4)
    return body + " " * (chars - len(body))


def _astral(units: int) -> str:
    """A string comparison ``units`` UTF-16 code units long, written with
    emoji (two units, one code point), so a length counted in code points
    would disagree with the renderer's."""
    head, tail = "datum.s === '", "'"
    room = units - len(head) - len(tail)
    return head + "\U0001f600" * (room // 2) + "a" * (room % 2) + tail


EXPRESSIONS_ADMIT: list[tuple[str, str, list[str]]] = [
    ("comparison", "datum.x > 5", ["x"]),
    ("altair_and", "((datum.a > 1) && (datum.c === 'x'))", ["a", "c"]),
    ("altair_not", "(!(datum.a > 1))", ["a"]),
    ("bracket_key_with_space", "(datum['my col'] > 1)", ["my col"]),
    ("bracket_key_with_dot", 'datum["a.b"] * 2', ["a.b"]),
    ("arithmetic", "datum.a * 2 + datum.b / 3 - datum.c % 4", ["a", "b", "c"]),
    ("altair_if", "if((datum.a > 1),'big','small')", ["a"]),
    ("nested_ternary", "datum.a > 0 ? 'pos' : datum.a < 0 ? 'neg' : 'zero'", ["a"]),
    ("jitter", "sqrt(-2*log(random()))*cos(2*PI*random())", []),
    ("format_concat", '"v=" + format(datum.b, ".1f")', ["b"]),
    (
        "string_functions",
        "upper(slice(trim(datum.name), 0, 3)) + lower(substring(datum.name, 1))",
        ["name"],
    ),
    ("altair_indexof", "(indexof(datum.c,'x') >= 0)", ["c"]),
    ("type_checks", "isValid(datum.a) && isNumber(toNumber(datum.a))", ["a"]),
    ("date_parts", "year(datum.t) - month(datum.t) + utchours(datum.t)", ["t"]),
    ("time_format", "timeFormat(toDate(datum.t), '%Y-%m')", ["t"]),
    ("days_since", "(now() - time(datum.t)) / 86400000", ["t"]),
    ("array_literal", "indexof(['a', 'b'], datum.c) >= 0", ["c"]),
    ("index_into_a_value", "split(datum.name, ' ')[0]", ["name"]),
    ("pad_literal_length", "pad(datum.code, 6, '0', 'left')", ["code"]),
    ("clamp", "clamp(datum.a, 0, 10)", ["a"]),
    ("constants", "PI * E + LN2 - SQRT1_2 + MAX_VALUE + LOG10E", []),
    ("literals", "datum.a === null || datum.b === true || datum.c !== false", ["a", "b", "c"]),
    ("number_forms", "datum.a * 1.5e3 + 0x1F - .5 + 0 + 2.", ["a"]),
    ("bitwise", "(datum.a & 1) | (datum.b ^ 2) << 1 >> 1 >>> 0 + ~datum.c", ["a", "b", "c"]),
    ("string_escapes", "datum.s === 'it\\'s \\u00e9 \\x41 \\u{1F600} \\n \\0'", ["s"]),
    ("unicode_text", "datum.s === 'caf\u00e9'", ["s"]),
    ("whitespace", "  datum.a\n>\t1 ", ["a"]),
    ("keyword_named_fields", "datum.if + datum.true", ["if", "true"]),
    ("ordinary_word_in_a_string", "datum.kind == 'window' || datum.kind == 'this'", ["kind"]),
    ("ternary_with_leading_decimal", "datum.a?.5:1", ["a"]),
    ("deepest_nesting", "(" * DEEPEST + "datum.a" + ")" * DEEPEST, ["a"]),
    ("longest", _long(MAX_EXPRESSION_CHARS), ["a"]),
    ("longest_in_utf16_units", _astral(MAX_EXPRESSION_CHARS), ["s"]),
]

EXPRESSIONS_REFUSE: list[tuple[str, str]] = [
    ("constructor_member", "datum.constructor"),
    ("proto_member", "datum.__proto__"),
    ("proto_bracket", "datum['__proto__']"),
    ("proto_hex_escape", "datum['\\x5f_proto__']"),
    ("proto_unicode_escape", "datum['\\u005f\\u005fproto\\u005f\\u005f']"),
    ("proto_code_point_escape", "datum['\\u{5f}_proto__']"),
    ("prototype_string", "indexof('prototype', datum.a)"),
    ("constructor_of_a_field", "datum.a.constructor"),
    ("constructor_call", "datum.a.constructor('return 1')()"),
    ("concatenated_key", "datum['con' + 'structor']"),
    ("computed_datum_key", "datum[datum.k]"),
    ("string_key_on_a_value", "split(datum.a, ',')['length']"),
    ("negative_index", "split(datum.a, ',')[-1]"),
    ("this", "this"),
    ("this_member", "this.x"),
    ("window", "window.location"),
    ("signal", "width * 2"),
    ("event", "event.x"),
    ("item", "item.datum"),
    ("bare_datum", "isObject(datum)"),
    ("eval", "eval('1')"),
    ("data_function", "data('x')"),
    ("indata_function", "indata('t', 'a', 1)"),
    ("scale_function", "scale('x', datum.a)"),
    ("regexp_function", "test(regexp('a+$'), datum.s)"),
    ("sequence_function", "length(sequence(0, 1e9))"),
    ("merge_function", "merge(datum.a, datum.b)"),
    ("warn_function", "warn('x')"),
    ("object_literal", "{a: 1}"),
    ("regex_literal", "/a+/"),
    ("assignment", "datum.a = 1"),
    ("compound_assignment", "datum.a += 1"),
    ("increment", "datum.a++"),
    ("decrement", "--datum.a"),
    ("member_call", "datum.f(1)"),
    ("call_of_a_call", "now()()"),
    ("escaped_identifier", "\\u0064atum.a"),
    ("non_ascii_identifier", "d\u00e4tum.a"),
    ("template_literal", "`x`"),
    ("octal_number", "017"),
    ("octal_escape", "'\\101'"),
    ("line_continuation", "'a\\\nb'"),
    ("unterminated_string", "'abc"),
    ("in_operator", "'a' in datum"),
    ("instanceof_operator", "datum.a instanceof Object"),
    ("typeof_operator", "typeof datum.a"),
    ("new_operator", "new Date()"),
    ("statement_separator", "datum.a; datum.b"),
    ("comma_operator", "(datum.a, datum.b)"),
    ("arrow_function", "x => x"),
    ("optional_chaining", "datum?.a"),
    ("nullish_coalescing", "datum.a ?? 1"),
    ("exponent_operator", "datum.a ** 2"),
    ("trailing_comma", "max(1, 2,)"),
    ("empty", ""),
    ("blank", "   "),
    ("too_long", _long(MAX_EXPRESSION_CHARS + 1)),
    ("too_long_in_utf16_units", _astral(MAX_EXPRESSION_CHARS + 1)),
    ("raw_line_separator_in_a_string", "datum.s === 'a\u2028b'"),
    ("too_deep", "(" * (DEEPEST + 1) + "datum.a" + ")" * (DEEPEST + 1)),
    ("too_deep_unary", "!" * MAX_EXPRESSION_DEPTH + "datum.a"),
    ("if_with_two_arguments", "if(datum.a, 1)"),
    ("pad_to_a_huge_length", "pad(datum.a, 1000000)"),
    ("pad_to_a_field_length", "pad(datum.a, datum.n)"),
    ("too_many_arguments", "max(" + ", ".join(["1"] * (MAX_EXPRESSION_ARGS + 1)) + ")"),
    ("array_too_long", "[" + ",".join(["1"] * 65) + "]"),
]


def _expressions() -> dict[str, object]:
    return {
        "admit": [
            {"name": name, "expression": text, "fields": sorted(fields)}
            for name, text, fields in EXPRESSIONS_ADMIT
        ],
        "refuse": [{"name": name, "expression": text} for name, text in EXPRESSIONS_REFUSE],
        "allowlist": {
            "functions": sorted(PROFILE.expression_functions),
            "constants": sorted(PROFILE.expression_constants),
            "max_chars": MAX_EXPRESSION_CHARS,
            "max_depth": MAX_EXPRESSION_DEPTH,
            "max_args": MAX_EXPRESSION_ARGS,
            "max_pad_length": MAX_PAD_LENGTH,
        },
    }


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> None:
    for name, spec in VALID.items():
        _write(HERE / "profile" / "valid" / f"{name}.json", spec)
    for name, case in INVALID.items():
        _write(HERE / "profile" / "invalid" / f"{name}.json", {"policy": "inline", **case})
    for name, payload in _expressions().items():
        _write(HERE / "profile" / "expressions" / f"{name}.json", payload)
    for scheme in CHART_TOKENS:
        _write(
            HERE / "theme" / f"{scheme}.json",
            {"tokens": CHART_TOKENS[scheme], "config": scheme_config(scheme)},
        )


if __name__ == "__main__":
    main()
