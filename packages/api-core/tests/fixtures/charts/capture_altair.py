"""Capture the Altair corpus: specs exactly as Altair writes them.

Altair is not a dependency of this repo; run this once in a scratch
environment that has it (``uv run --with altair --with pandas``) and commit the
output. The files are evidence of a writer's spelling and are never edited::

    uv run --no-project --with altair==6.3.0 --with pandas \
        python packages/api-core/tests/fixtures/charts/capture_altair.py
"""

from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd

HERE = Path(__file__).resolve().parent / "profile" / "altair"


def main() -> None:
    df = pd.DataFrame(
        {
            "a": [1, 2, 3, 4],
            "b": [4.0, 5.5, 6.25, 3.0],
            "c": ["x", "y", "x", "y"],
            "t": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
        }
    )
    brush = alt.selection_interval(encodings=["x"])
    legend = alt.selection_point(fields=["c"], bind="legend")
    base = alt.Chart(df)
    charts = {
        "line_interactive": base.mark_line()
        .encode(x="t:T", y="b", color="c", tooltip=["a", "b"])
        .interactive(),
        "brush_condition": base.mark_point(tooltip=True)
        .encode(x="a", y="b", color=alt.condition(brush, "c:N", alt.value("lightgray")))
        .add_params(brush),
        "regression_layer": base.mark_point().encode(x="a", y="b")
        + base.mark_line().transform_regression("a", "b").encode(x="a", y="b"),
        "concat_brush_filter": base.mark_point().encode(x="a", y="b").add_params(brush)
        | base.mark_bar().encode(x="c", y="sum(b)").transform_filter(brush),
        "histogram": base.mark_bar().encode(alt.X("b", bin=alt.Bin(maxbins=20)), y="count()"),
        "legend_selection": base.mark_line()
        .encode(
            x="a", y="b", color="c", opacity=alt.condition(legend, alt.value(1), alt.value(0.2))
        )
        .add_params(legend),
        "facet": base.mark_point().encode(x="a", y="b").facet(column="c"),
        "donut": base.mark_arc(innerRadius=40).encode(theta="sum(b)", color="c"),
        "errorband_mean": base.mark_errorband(extent="ci").encode(x="a", y="b")
        + base.mark_line().encode(x="a", y="mean(b)"),
        "boxplot": base.mark_boxplot().encode(x="c", y="b"),
        "density": base.transform_density("b", as_=["b", "density"])
        .mark_area()
        .encode(x="b:Q", y="density:Q"),
        "window_cumulative": base.transform_window(cum="sum(b)", sort=[{"field": "a"}])
        .mark_line()
        .encode(x="a", y="cum:Q"),
        "heatmap": base.mark_rect().encode(x="a:O", y="c:N", color="b:Q"),
        "fold": base.transform_fold(["a", "b"])
        .mark_line()
        .encode(x="c:N", y="value:Q", color="key:N"),
        "filter_predicate": base.transform_filter(alt.FieldGTPredicate(field="a", gt=1))
        .mark_point()
        .encode(x="a", y="b"),
        "text": base.mark_text(dy=-5).encode(x="a", y="b", text="c"),
        "timeunit_bar": base.mark_bar().encode(x="yearmonthdate(t):O", y="sum(b)"),
        # Expressions, as Altair spells them: a string filter, an `alt.datum`
        # comparison, a calculate, `alt.condition` and `alt.when` over a datum
        # test, and the jitter recipe (random draws).
        "filter_expression": base.mark_point().encode(x="a", y="b").transform_filter("datum.a > 1"),
        "filter_datum": base.mark_point()
        .encode(x="a", y="b")
        .transform_filter((alt.datum.a > 1) & (alt.datum.c == "x")),
        "calculate_expression": base.transform_calculate(d="datum.b * 2")
        .mark_bar()
        .encode(x="c:N", y="sum(d):Q"),
        "condition_datum": base.mark_point().encode(
            x="a",
            y="b",
            color=alt.condition(alt.datum.b > 5, alt.value("crimson"), alt.value("gray")),
        ),
        "when_datum": base.mark_point().encode(
            x="a",
            y="b",
            color=alt.when(alt.datum.a > 2).then(alt.value("crimson")).otherwise(alt.value("gray")),
        ),
        "jitter_calculate": base.transform_calculate(
            jitter="sqrt(-2*log(random()))*cos(2*PI*random())"
        )
        .mark_circle()
        .encode(x="c:N", y="jitter:Q"),
    }
    HERE.mkdir(parents=True, exist_ok=True)
    for name, chart in charts.items():
        (HERE / f"{name}.json").write_text(
            json.dumps(chart.to_dict(), indent=2, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    main()
