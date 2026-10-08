"""Expressions: the grammar, the allowlist and where the profile reads them.

The cases live in the shared corpus (``fixtures/charts/profile/expressions``),
which the renderer's guard (``packages/ui/src/charts/expression.test.ts``) walks
too, so an expression one side admits and the other refuses is a red case on
one of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_core.charts import HOSTED, INLINE, PROFILE, ChartSpecError, default_profile, validate
from alkera_core.charts.profile import (
    MAX_EXPRESSION_ARGS,
    MAX_EXPRESSION_CHARS,
    MAX_EXPRESSION_DEPTH,
    MAX_PAD_LENGTH,
    ExpressionError,
    check_expression,
)

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "charts" / "profile" / "expressions"


def _load(name: str) -> Any:
    return json.loads((CORPUS / f"{name}.json").read_text())


ADMIT = [pytest.param(case, id=case["name"]) for case in _load("admit")]
REFUSE = [pytest.param(case, id=case["name"]) for case in _load("refuse")]


def _slots(expression: str) -> dict[str, dict[str, Any]]:
    """The three places a spec carries an expression, over a host table so the
    case is about the expression, not about which columns exist."""
    base: dict[str, Any] = {"data": {"name": "t"}, "mark": "point"}
    return {
        "calculate": {**base, "transform": [{"calculate": expression, "as": "out"}]},
        "filter": {**base, "transform": [{"filter": expression}]},
        "test": {
            **base,
            "encoding": {
                "color": {"condition": {"test": expression, "value": "red"}, "value": "blue"}
            },
        },
    }


SLOT_PATHS = {
    "calculate": "transform[0].calculate",
    "filter": "transform[0].filter",
    "test": "encoding.color.condition.test",
}


def test_the_corpus_is_present_so_the_parametrized_cases_cannot_pass_vacuously() -> None:
    assert len(ADMIT) >= 30
    assert len(REFUSE) >= 60


@pytest.mark.parametrize("case", ADMIT)
def test_an_admitted_expression_reports_the_row_fields_it_reads(case: dict[str, Any]) -> None:
    assert check_expression(case["expression"]) == case["fields"]


@pytest.mark.parametrize("slot", sorted(SLOT_PATHS))
@pytest.mark.parametrize("case", ADMIT)
def test_an_admitted_expression_is_admitted_in_every_slot(case: dict[str, Any], slot: str) -> None:
    spec = _slots(case["expression"])[slot]
    assert validate(spec, policy=HOSTED).persisted()["transform" if slot != "test" else "encoding"]


@pytest.mark.parametrize("case", REFUSE)
def test_a_refused_expression_is_refused_without_echoing_it(case: dict[str, Any]) -> None:
    with pytest.raises(ExpressionError) as refused:
        check_expression(case["expression"])
    text = case["expression"].strip()
    if len(text) > 3:
        assert text not in str(refused.value)


@pytest.mark.parametrize("slot", sorted(SLOT_PATHS))
@pytest.mark.parametrize("case", REFUSE)
def test_a_refused_expression_is_refused_at_its_slot(case: dict[str, Any], slot: str) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate(_slots(case["expression"])[slot], policy=HOSTED)
    assert refused.value.path == SLOT_PATHS[slot]


def test_the_shared_allowlist_is_the_profiles_own() -> None:
    allowlist = _load("allowlist")
    assert allowlist == {
        "functions": sorted(PROFILE.expression_functions),
        "constants": sorted(PROFILE.expression_constants),
        "max_chars": MAX_EXPRESSION_CHARS,
        "max_depth": MAX_EXPRESSION_DEPTH,
        "max_args": MAX_EXPRESSION_ARGS,
        "max_pad_length": MAX_PAD_LENGTH,
    }


@pytest.mark.parametrize(
    "name",
    ["data", "indata", "scale", "invert", "regexp", "test", "sequence", "merge", "warn"],
)
def test_functions_that_reach_state_or_allocate_without_bound_are_not_allowed(name: str) -> None:
    assert name not in PROFILE.expression_functions


@pytest.mark.parametrize(
    ("spec", "path"),
    [
        pytest.param(
            {"transform": [{"calculate": "datum.missing * 2", "as": "out"}], "mark": "point"},
            "transform[0].calculate",
            id="calculate",
        ),
        pytest.param(
            {"transform": [{"filter": "datum.missing > 1"}], "mark": "point"},
            "transform[0].filter",
            id="filter",
        ),
        pytest.param(
            {
                "mark": "point",
                "encoding": {
                    "color": {
                        "condition": {"test": "datum.missing > 1", "value": "red"},
                        "value": "blue",
                    }
                },
            },
            "encoding.color.condition.test",
            id="condition",
        ),
    ],
)
def test_a_field_an_expression_reads_is_bound_like_any_other(
    spec: dict[str, Any], path: str
) -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate({"data": {"values": [{"a": 1}]}, **spec}, policy=INLINE)
    assert refused.value.path == path
    assert "missing" not in str(refused.value)


def test_a_bracketed_key_binds_to_the_column_of_that_exact_name() -> None:
    rows = {"values": [{"a.b": 1}]}
    spec = {"data": rows, "transform": [{"filter": "datum['a.b'] > 0"}], "mark": "point"}
    assert validate(spec)
    nested = {"data": rows, "transform": [{"filter": "datum.a > 0"}], "mark": "point"}
    with pytest.raises(ChartSpecError):
        validate(nested)


def test_a_calculated_field_is_readable_by_the_encodings_after_it() -> None:
    spec = {
        "data": {"values": [{"a": 1}]},
        "transform": [{"calculate": "datum.a * 2", "as": "double"}],
        "mark": "point",
        "encoding": {"y": {"field": "double", "type": "quantitative"}},
    }
    assert validate(spec).produced == {"double"}


def test_a_calculate_must_name_its_output() -> None:
    with pytest.raises(ChartSpecError) as refused:
        validate(
            {"data": {"name": "t"}, "transform": [{"calculate": "1"}], "mark": "point"},
            policy=HOSTED,
        )
    assert refused.value.path == "transform[0].as"


def test_an_expression_inside_a_logical_composition_is_checked() -> None:
    def spec(expression: str) -> dict[str, Any]:
        return {
            "data": {"name": "t"},
            "mark": "point",
            "transform": [{"filter": {"or": [{"not": expression}, {"field": "a", "gt": 1}]}}],
        }

    assert validate(spec("datum.a < 3"), policy=HOSTED)
    with pytest.raises(ChartSpecError) as refused:
        validate(spec("data('x')"), policy=HOSTED)
    assert refused.value.path == "transform[0].filter.or[0].not"


def test_a_function_is_admitted_by_registering_it() -> None:
    extended = default_profile()
    extended.expression_functions.add("luminance")
    assert check_expression("luminance('red') > 0.5", extended) == []
    with pytest.raises(ExpressionError):
        check_expression("luminance('red') > 0.5")


def test_a_constant_is_admitted_by_registering_it() -> None:
    extended = default_profile()
    extended.expression_constants.add("TAU")
    assert check_expression("TAU / 2", extended) == []
    with pytest.raises(ExpressionError):
        check_expression("TAU / 2")


def test_the_length_is_measured_in_utf16_units_as_the_renderer_measures_it() -> None:
    # An emoji is one code point and two UTF-16 units.
    prefix = "datum.s === '"
    fits = prefix + "\U0001f600" * ((MAX_EXPRESSION_CHARS - len(prefix) - 1) // 2) + "'"
    assert len(fits.encode("utf-16-le")) // 2 <= MAX_EXPRESSION_CHARS
    assert check_expression(fits) == ["s"]
    over = prefix + "\U0001f600" * ((MAX_EXPRESSION_CHARS - len(prefix)) // 2 + 1) + "'"
    assert len(over) <= MAX_EXPRESSION_CHARS
    with pytest.raises(ExpressionError, match="longer"):
        check_expression(over)


def test_a_non_string_is_refused() -> None:
    with pytest.raises(ExpressionError):
        check_expression(5)  # type: ignore[arg-type]
