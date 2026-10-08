"""``compile_query``: a slot becomes the engine's placeholder and its value
travels beside the statement — the SQL text never carries a value, whatever
the value says; a value of the wrong shape is refused before anything is
compiled; an engine with no bound-parameter path is refused, not rendered."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from alkera_core.schemas.objects import (
    ENGINE_STYLES,
    QueryCompileError,
    QueryParamDeclaration,
    compile_query,
    placeholders_of,
)
from alkera_core.schemas.objects.query_params import PARAM_TYPES

TEMPLATE = "select * from orders where customer = {customer} and x-{n} > 0"

INJECTIONS = [
    pytest.param("' OR 1=1 --", id="quote-or-comment"),
    pytest.param("\\' OR 1=1 --", id="backslash-quote-or-comment"),
    pytest.param("'; DROP TABLE orders; --", id="statement-terminator"),
    pytest.param("acme') UNION SELECT password FROM users --", id="union"),
    pytest.param("{n:String}", id="a-clickhouse-placeholder-as-a-value"),
    pytest.param("%(n)s", id="a-pyformat-placeholder-as-a-value"),
]


# --------------------------------------------------------------------------- #
# the statement never carries a value
# --------------------------------------------------------------------------- #


#: Every engine with a binding path, and the exact text ``TEMPLATE`` compiles to on it.
#: Keyed by engine so the matrix below can be checked against ``ENGINE_STYLES`` itself:
#: an engine added there without a row here would ship with no injection coverage.
COMPILED_TEMPLATE: dict[str, str] = {
    "postgres": "select * from orders where customer = %(customer)s and x-%(n)s > 0",
    "redshift": "select * from orders where customer = %(customer)s and x-%(n)s > 0",
    "clickhouse": "select * from orders where customer = {customer:String} and x-{n:Int64} > 0",
    "tinybird": "select * from orders where customer = {{String(customer)}} and x-{{Int64(n)}} > 0",
    "duckdb": "select * from orders where customer = $customer and x-$n > 0",
    "duckdb_local": "select * from orders where customer = $customer and x-$n > 0",
    "sqlite": "select * from orders where customer = :customer and x-:n > 0",
}


def test_the_injection_matrix_covers_every_engine_with_a_binding_path() -> None:
    """The matrix below is a guarantee only if it is exhaustive."""
    assert set(COMPILED_TEMPLATE) == set(ENGINE_STYLES)


@pytest.mark.parametrize("value", INJECTIONS)
@pytest.mark.parametrize(
    "engine", [pytest.param(engine, id=engine) for engine in COMPILED_TEMPLATE]
)
def test_an_injection_string_is_bound_inert_on_every_engine(value: str, engine: str) -> None:
    bound = compile_query(TEMPLATE, {"customer": value, "n": -5}, engine)
    # The text is fixed by the template and the engine alone: a value that
    # leaked into it would change it.
    assert bound.sql == COMPILED_TEMPLATE[engine]
    assert "--" not in bound.sql and ";" not in bound.sql
    # The raw value travels untouched, to be bound by the driver.
    assert bound.params == {"customer": value, "n": -5}
    assert bound.placeholders == ("customer", "n")
    assert bound.engine == engine


def test_a_negative_number_cannot_open_a_comment() -> None:
    """``x-{n}`` with ``n=-5`` used to render ``x--5``, commenting out the rest
    of the line; the placeholder keeps the subtraction and the value apart."""
    bound = compile_query("select x-{n} from t where keep = 1", {"n": -5}, "postgres")
    assert bound.sql == "select x-%(n)s from t where keep = 1"
    assert bound.params == {"n": -5}


def test_the_sql_is_identical_for_every_value_of_a_slot() -> None:
    texts = {
        compile_query(TEMPLATE, {"customer": value, "n": 1}, "postgres").sql
        for value in ("acme", "' OR 1=1 --", "x" * 100, "")
    }
    assert len(texts) == 1


def test_a_template_without_slots_compiles_to_itself_with_no_params() -> None:
    bound = compile_query("select 1 where x like '%a%'", {}, "postgres")
    assert bound.sql == "select 1 where x like '%a%'" and bound.params == {}
    assert bound.placeholders == ()


def test_a_literal_percent_is_doubled_only_when_pyformat_values_are_bound() -> None:
    assert (
        compile_query("select * from t where a like '%x%' and b = {b}", {"b": 1}, "postgres").sql
        == "select * from t where a like '%%x%%' and b = %(b)s"
    )
    assert (
        compile_query("select * from t where a like '%x%' and b = {b}", {"b": 1}, "clickhouse").sql
        == "select * from t where a like '%x%' and b = {b:Int64}"
    )
    assert (
        compile_query("select * from t where a like '%x%' and b = {b}", {"b": 1}, "duckdb").sql
        == "select * from t where a like '%x%' and b = $b"
    )


def test_braces_that_are_not_a_slot_are_left_alone() -> None:
    template = "select '{\"k\": 1}'::json, {x:String}, {a}"
    bound = compile_query(template, {"a": "v"}, "clickhouse")
    assert bound.sql == "select '{\"k\": 1}'::json, {x:String}, {a:String}"
    assert bound.params == {"a": "v"}


# --------------------------------------------------------------------------- #
# engines
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("snowflake", id="snowflake"),
        pytest.param("bigquery", id="bigquery"),
        pytest.param("mysql", id="mysql"),
        pytest.param("trino", id="trino"),
        pytest.param("", id="empty"),
        pytest.param("nonsense", id="unknown"),
    ],
)
def test_an_engine_without_a_binding_path_is_refused_never_rendered(engine: str) -> None:
    with pytest.raises(QueryCompileError, match="bound parameters are not supported"):
        compile_query(TEMPLATE, {"customer": "acme", "n": 1}, engine)


def test_engine_names_are_case_and_space_insensitive() -> None:
    assert compile_query("select {a}", {"a": 1}, " Postgres ").sql == "select %(a)s"
    assert set(ENGINE_STYLES) >= {"postgres", "tinybird", "clickhouse", "duckdb", "sqlite"}


# --------------------------------------------------------------------------- #
# lists and date ranges
# --------------------------------------------------------------------------- #


def test_a_list_expands_to_one_placeholder_per_item_in_parentheses() -> None:
    bound = compile_query("select * from t where id in {ids}", {"ids": [3, 1, 2]}, "postgres")
    assert bound.sql == "select * from t where id in (%(ids_0)s, %(ids_1)s, %(ids_2)s)"
    assert bound.params == {"ids_0": 3, "ids_1": 1, "ids_2": 2}
    ch = compile_query("select * from t where id in {ids}", {"ids": ["a", "b"]}, "clickhouse")
    assert ch.sql == "select * from t where id in ({ids_0:String}, {ids_1:String})"
    tb = compile_query("select * from t where id in {ids}", {"ids": ["a", "b"]}, "tinybird")
    assert tb.sql == "select * from t where id in ({{String(ids_0)}}, {{String(ids_1)}})"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param([], "empty list", id="empty"),
        pytest.param([1, "a"], "mixes value types", id="mixed"),
        pytest.param([[1]], "nest a list", id="nested"),
        pytest.param([None], "cannot be bound", id="null-item"),
        pytest.param([{"a": 1}], "is an object", id="object-item"),
        pytest.param(list(range(1001)), "more than 1000", id="too-long"),
    ],
)
def test_a_malformed_list_is_refused(value: Any, message: str) -> None:
    with pytest.raises(QueryCompileError, match=message):
        compile_query("select {ids}", {"ids": value}, "postgres")


RANGE_DECL = [{"name": "r", "type": "daterange"}]


def test_a_daterange_binds_start_and_end_as_dates() -> None:
    bound = compile_query(
        "select * from t where d between {r.start} and {r.end}",
        {"r": {"start": "2026-01-01", "end": "2026-02-01"}},
        "postgres",
        declarations=RANGE_DECL,
    )
    assert bound.sql == "select * from t where d between %(r__start)s and %(r__end)s"
    assert bound.params == {"r__start": date(2026, 1, 1), "r__end": date(2026, 2, 1)}
    as_list = compile_query(
        "select {r.start}", {"r": ["2026-01-01", "2026-01-01"]}, "postgres", declarations=RANGE_DECL
    )
    assert as_list.params == {"r__start": date(2026, 1, 1)}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param("2026-01-01", "must be a date range", id="a-string"),
        pytest.param({"start": "2026-01-01"}, "must be {start, end}", id="missing-end"),
        pytest.param(
            {"start": "2026-01-01", "end": "2026-02-01", "tz": "UTC"},
            "must be {start, end}",
            id="extra-key",
        ),
        pytest.param(
            ["2026-01-01", "2026-02-01", "2026-03-01"], "must be a date range", id="3-list"
        ),
        pytest.param(
            {"start": "2026-03-01", "end": "2026-01-01"}, "start is after end", id="reversed"
        ),
        pytest.param(
            {"start": "2026-13-01", "end": "2026-12-01"}, "not a calendar date", id="bad-date"
        ),
        pytest.param({"start": "jan 1", "end": "2026-12-01"}, "must be a date", id="not-iso"),
        pytest.param({"start": 20260101, "end": 20260201}, "must be a date", id="ints"),
    ],
)
def test_a_daterange_of_the_wrong_shape_is_refused(value: Any, message: str) -> None:
    with pytest.raises(QueryCompileError, match=message):
        compile_query(
            "select {r.start}, {r.end}", {"r": value}, "postgres", declarations=RANGE_DECL
        )


def test_a_bare_slot_on_a_daterange_and_a_part_on_a_scalar_are_both_refused() -> None:
    with pytest.raises(QueryCompileError, match=r"use \{r.start\} and \{r.end\}"):
        compile_query(
            "select {r}",
            {"r": {"start": "2026-01-01", "end": "2026-01-02"}},
            "postgres",
            declarations=RANGE_DECL,
        )
    with pytest.raises(QueryCompileError, match="needs a daterange"):
        compile_query("select {n.start}", {"n": 1}, "postgres")


def test_an_undeclared_object_is_refused() -> None:
    with pytest.raises(QueryCompileError, match="only a declared daterange"):
        compile_query("select {r}", {"r": {"start": "2026-01-01", "end": "2026-01-02"}}, "postgres")


# --------------------------------------------------------------------------- #
# declared types
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("decl", "value", "bound"),
    [
        pytest.param({"name": "p", "type": "string"}, "acme", "acme", id="string"),
        pytest.param({"name": "p", "type": "integer"}, 5, 5, id="integer"),
        pytest.param({"name": "p", "type": "integer"}, "-5", -5, id="integer-from-text"),
        pytest.param({"name": "p", "type": "number"}, 2.5, 2.5, id="number"),
        pytest.param({"name": "p", "type": "number"}, 3, 3, id="number-from-int"),
        pytest.param({"name": "p", "type": "number"}, "1.25", 1.25, id="number-from-text"),
        pytest.param({"name": "p", "type": "number"}, Decimal("0.5"), 0.5, id="number-decimal"),
        pytest.param({"name": "p", "type": "boolean"}, True, True, id="boolean"),
        pytest.param({"name": "p", "type": "boolean"}, "false", False, id="boolean-from-text"),
        pytest.param({"name": "p", "type": "date"}, "2026-09-06", date(2026, 9, 6), id="date"),
        pytest.param(
            {"name": "p", "type": "date"},
            datetime(2026, 9, 6, 23, 59, tzinfo=UTC),
            date(2026, 9, 6),
            id="date-from-datetime",
        ),
        pytest.param(
            {"name": "p", "type": "enum", "enum_values": ["eu", "us"]}, "eu", "eu", id="enum"
        ),
    ],
)
def test_a_declared_value_of_the_right_shape_binds(
    decl: dict[str, Any], value: Any, bound: Any
) -> None:
    result = compile_query("select {p}", {"p": value}, "postgres", declarations=[decl])
    assert result.sql == "select %(p)s"
    assert result.params == {"p": bound}
    assert type(result.params["p"]) is type(bound)


@pytest.mark.parametrize(
    ("decl", "value", "message"),
    [
        pytest.param({"name": "p", "type": "string"}, 5, "must be a string", id="string-int"),
        pytest.param({"name": "p", "type": "string"}, "a\x00b", "NUL", id="string-nul"),
        pytest.param({"name": "p", "type": "string"}, "x" * 8193, "longer than", id="string-long"),
        pytest.param({"name": "p", "type": "integer"}, True, "not a boolean", id="integer-bool"),
        pytest.param(
            {"name": "p", "type": "integer"}, "5.5", "must be an integer", id="integer-float-text"
        ),
        pytest.param(
            {"name": "p", "type": "integer"}, 5.5, "must be an integer", id="integer-float"
        ),
        pytest.param(
            {"name": "p", "type": "integer"}, "5; drop", "must be an integer", id="integer-junk"
        ),
        pytest.param({"name": "p", "type": "integer"}, 2**63, "64-bit", id="integer-range"),
        pytest.param({"name": "p", "type": "number"}, float("nan"), "finite", id="number-nan"),
        pytest.param({"name": "p", "type": "number"}, "inf", "finite", id="number-inf-text"),
        pytest.param({"name": "p", "type": "number"}, "1e", "must be a number", id="number-junk"),
        pytest.param({"name": "p", "type": "number"}, False, "not a boolean", id="number-bool"),
        pytest.param({"name": "p", "type": "boolean"}, 1, "true or false", id="boolean-int"),
        pytest.param({"name": "p", "type": "boolean"}, "yes", "true or false", id="boolean-yes"),
        pytest.param({"name": "p", "type": "date"}, "2026-9-6", "must be a date", id="date-loose"),
        pytest.param(
            {"name": "p", "type": "date"}, "20260906", "must be a date", id="date-compact"
        ),
        pytest.param(
            {"name": "p", "type": "date"}, "2026-02-30", "not a calendar date", id="date-feb30"
        ),
        pytest.param(
            {"name": "p", "type": "enum", "enum_values": ["eu", "us"]},
            "apac",
            "one of the declared values",
            id="enum-outside",
        ),
        pytest.param(
            {"name": "p", "type": "enum", "enum_values": ["eu"]},
            1,
            "one of the declared",
            id="enum-int",
        ),
        pytest.param({"name": "p", "type": "enum"}, "eu", "no values", id="enum-empty"),
    ],
)
def test_a_declared_value_of_the_wrong_shape_is_refused(
    decl: dict[str, Any], value: Any, message: str
) -> None:
    with pytest.raises(QueryCompileError, match=message):
        compile_query("select {p}", {"p": value}, "postgres", declarations=[decl])


@pytest.mark.parametrize(
    ("decl", "message"),
    [
        pytest.param({"name": "1p"}, "invalid slot", id="bad-name"),
        pytest.param({"name": "p", "type": "uuid"}, "unknown type", id="bad-type"),
        pytest.param(
            {"name": "p", "type": "enum", "enum_values": [1]}, "list of strings", id="bad-enum"
        ),
        pytest.param({"name": "p", "required": "yes"}, "must be a boolean", id="bad-required"),
    ],
)
def test_a_malformed_declaration_is_refused(decl: dict[str, Any], message: str) -> None:
    with pytest.raises(QueryCompileError, match=message):
        compile_query("select {p}", {"p": "x"}, "postgres", declarations=[decl])


def test_a_declaration_repeated_is_refused() -> None:
    with pytest.raises(QueryCompileError, match="declared twice"):
        compile_query(
            "select {p}", {"p": "x"}, "postgres", declarations=[{"name": "p"}, {"name": "p"}]
        )


def test_an_optional_declared_slot_left_out_binds_null() -> None:
    bound = compile_query(
        "select {p}",
        {},
        "postgres",
        declarations=[QueryParamDeclaration(name="p", type="string", required=False)],
    )
    assert bound.sql == "select %(p)s" and bound.params == {"p": None}


@pytest.mark.parametrize("engine", ["clickhouse", "tinybird"])
def test_a_null_cannot_be_bound_on_the_brace_engines(engine: str) -> None:
    with pytest.raises(QueryCompileError, match="IS NULL"):
        compile_query("select {p}", {"p": None}, engine)
    assert compile_query("select {p}", {"p": None}, "postgres").params == {"p": None}


def test_a_boolean_on_clickhouse_is_a_uint8() -> None:
    bound = compile_query("select {p}, {q}", {"p": True, "q": False}, "clickhouse")
    assert bound.sql == "select {p:UInt8}, {q:UInt8}" and bound.params == {"p": 1, "q": 0}


def test_a_boolean_on_tinybird_stays_a_boolean_slot() -> None:
    """Tinybird has a ``Boolean`` template type, so the bool survives as a bool rather
    than being folded to ClickHouse's 0/1 — and the driver renders it ``true``/``false``,
    because Tinybird's own Boolean reads any word it does not recognize as true."""
    bound = compile_query("select {p}, {q}", {"p": True, "q": False}, "tinybird")
    assert bound.sql == "select {{Boolean(p)}}, {{Boolean(q)}}"
    assert bound.params == {"p": True, "q": False}


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        pytest.param("clickhouse", "select {d:Date}", id="clickhouse"),
        pytest.param("tinybird", "select {{Date(d)}}", id="tinybird"),
    ],
)
def test_a_date_keeps_its_declared_type_on_both_brace_engines(engine: str, expected: str) -> None:
    bound = compile_query(
        "select {d}", {"d": "2026-01-02"}, engine, declarations=[{"name": "d", "type": "date"}]
    )
    assert bound.sql == expected and bound.params == {"d": date(2026, 1, 2)}


# --------------------------------------------------------------------------- #
# Tinybird: its own template syntax, not ClickHouse's placeholder
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("declaration", "value", "expected_sql", "expected_value"),
    [
        pytest.param({"type": "string"}, "acme", "select {{String(p)}}", "acme", id="string"),
        pytest.param({"type": "integer"}, 7, "select {{Int64(p)}}", 7, id="integer"),
        pytest.param(
            {"type": "integer"},
            2**63 - 1,
            "select {{Int64(p)}}",
            2**63 - 1,
            id="integer-int64-max",
        ),
        pytest.param({"type": "number"}, 1.5, "select {{Float64(p)}}", 1.5, id="number"),
        pytest.param({"type": "number"}, 7, "select {{Int64(p)}}", 7, id="number-given-an-int"),
        pytest.param({"type": "boolean"}, True, "select {{Boolean(p)}}", True, id="boolean-true"),
        pytest.param(
            {"type": "boolean"}, False, "select {{Boolean(p)}}", False, id="boolean-false"
        ),
        pytest.param(
            {"type": "date"}, "2026-01-02", "select {{Date(p)}}", date(2026, 1, 2), id="date"
        ),
        pytest.param(
            {"type": "datetime"},
            "2026-01-02 03:04:05",
            "select {{DateTime(p)}}",
            datetime(2026, 1, 2, 3, 4, 5),
            id="datetime",
        ),
        pytest.param(
            {"type": "enum", "enum_values": ["emea"]},
            "emea",
            "select {{String(p)}}",
            "emea",
            id="enum",
        ),
    ],
)
def test_every_declared_scalar_kind_compiles_to_a_tinybird_slot(
    declaration: dict[str, Any], value: Any, expected_sql: str, expected_value: Any
) -> None:
    """One row per ``ParamType`` that binds a scalar — Tinybird has a template type
    function for each, and it re-validates the value against that type on its own side."""
    bound = compile_query(
        "select {p}", {"p": value}, "tinybird", declarations=[{"name": "p", **declaration}]
    )
    assert bound.sql == expected_sql
    assert bound.params == {"p": expected_value}


def test_the_tinybird_slot_matrix_covers_every_scalar_param_type() -> None:
    """``daterange`` is the only declared type with no scalar row: it is two ``date``
    slots, covered below. Every other declared type must appear above."""
    covered = {"string", "integer", "number", "boolean", "date", "datetime", "enum"}
    assert covered == PARAM_TYPES - {"daterange"}


@pytest.mark.parametrize(
    ("value", "expected_sql"),
    [
        pytest.param("acme", "select {{String(p)}}", id="string"),
        pytest.param("2026-01-01", "select {{String(p)}}", id="date-shaped-string-stays-a-string"),
        pytest.param(7, "select {{Int64(p)}}", id="integer"),
        pytest.param(1.5, "select {{Float64(p)}}", id="number"),
        pytest.param(True, "select {{Boolean(p)}}", id="boolean"),
        pytest.param(date(2026, 1, 1), "select {{Date(p)}}", id="date-object"),
        pytest.param(datetime(2026, 1, 1, 3, 4, tzinfo=UTC), "select {{Date(p)}}", id="datetime"),
    ],
)
def test_every_inferred_scalar_kind_compiles_to_a_tinybird_slot(
    value: Any, expected_sql: str
) -> None:
    assert compile_query("select {p}", {"p": value}, "tinybird").sql == expected_sql


def test_a_tinybird_daterange_binds_both_ends_as_date_slots() -> None:
    bound = compile_query(
        "where d between {r.start} and {r.end}",
        {"r": {"start": "2026-01-01", "end": "2026-01-31"}},
        "tinybird",
        declarations=[{"name": "r", "type": "daterange"}],
    )
    assert bound.sql == "where d between {{Date(r__start)}} and {{Date(r__end)}}"
    assert bound.params == {"r__start": date(2026, 1, 1), "r__end": date(2026, 1, 31)}


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("select {{sql_and(a=Int64(x))}}, {p}", id="a-directive-that-builds-sql"),
        pytest.param(
            "select {% if x %}1{% end %}, {p}",
            id="a-control-block",
        ),
        pytest.param("select {p}, {{columns(cols)}}", id="a-directive-after-the-slot"),
    ],
)
def test_a_tinybird_template_may_not_carry_a_directive_of_its_own(template: str) -> None:
    """A parametrised statement is sent to Tinybird AS a template, so the author's own
    ``{{...}}`` would be read by the same engine — and Tinybird's other directives write
    text into the SQL (``{{sql_and(a=Int64(x))}}`` becomes ``a = toInt64('1')``) instead
    of binding beside it. The compiler refuses rather than hand that text a value."""
    with pytest.raises(QueryCompileError, match="may not contain"):
        compile_query(template, {"p": "v"}, "tinybird")


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("select {{sql_and(a=Int64(x))}}", id="directive"),
        pytest.param("select {% if x %}1{% end %}", id="control-block"),
    ],
)
def test_a_tinybird_statement_with_no_slots_is_left_exactly_as_written(template: str) -> None:
    """With nothing to bind there is no template mode to enter, so the braces are the
    author's own SQL and reach Tinybird unmarked (where they are a syntax error, which is
    Tinybird's answer to give). The refusal above is about a statement Alkera would MARK."""
    assert compile_query(template, {}, "tinybird").sql == template


def test_a_tinybird_value_that_looks_like_a_slot_is_still_only_a_value() -> None:
    """The property the whole transport exists for: the compiled text is the same
    whatever the value says, so a value spelling a slot cannot become one."""
    texts = {
        compile_query("select {p}", {"p": value}, "tinybird").sql
        for value in ("acme", "{{String(other)}}", "{{sql_and(a=Int64(x))}}", "{% if 1 %}")
    }
    assert texts == {"select {{String(p)}}"}


# --------------------------------------------------------------------------- #
# inferred types, and the template/params fit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        pytest.param("acme", "{p:String}", id="string"),
        pytest.param("2026-01-01", "{p:String}", id="date-shaped-string-stays-a-string"),
        pytest.param(7, "{p:Int64}", id="integer"),
        pytest.param(1.5, "{p:Float64}", id="number"),
        pytest.param(date(2026, 1, 1), "{p:Date}", id="date-object"),
    ],
)
def test_an_undeclared_value_takes_its_json_type(value: Any, placeholder: str) -> None:
    bound = compile_query("select {p}", {"p": value}, "clickhouse")
    assert bound.sql == f"select {placeholder}"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(object(), id="object"),
        pytest.param(b"bytes", id="bytes"),
        pytest.param(float("inf"), id="inf"),
        pytest.param("a\x00b", id="nul"),
    ],
)
def test_an_undeclared_value_with_no_safe_binding_is_refused(value: Any) -> None:
    with pytest.raises(QueryCompileError):
        compile_query("select {p}", {"p": value}, "postgres")


@pytest.mark.parametrize(
    ("template", "params", "message"),
    [
        pytest.param("select {a}", {}, "missing value for a", id="missing"),
        pytest.param("select {a}, {b}", {"a": 1}, "missing value for b", id="one-missing"),
        pytest.param("select {a}, {b}", {}, "missing value for a, b", id="all-missing"),
        pytest.param("select 1", {"a": 1}, "no placeholder named a", id="unknown-param"),
        pytest.param("select {a}", {"a": 1, "b": 2}, "no placeholder named b", id="extra-param"),
        pytest.param("select {a}", {"a": 1, "b c": 2}, "invalid parameter name", id="bad-name"),
        pytest.param(
            "select {a}", {"a": 1, "a_0": 2}, "no placeholder named a_0", id="collision-name"
        ),
    ],
)
def test_a_template_and_params_that_do_not_fit_are_refused(
    template: str, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(QueryCompileError, match=message):
        compile_query(template, params, "postgres")


def test_a_compiled_name_clash_is_refused() -> None:
    """A list slot ``ids`` compiles to ``ids_0``; a declared slot of that very
    name would collide with it, so the clash is refused rather than binding one
    value over the other."""
    with pytest.raises(QueryCompileError, match="used twice"):
        compile_query(
            "select {ids}, {ids_0}",
            {"ids": [1, 2], "ids_0": 9},
            "postgres",
            declarations=[{"name": "ids_0", "type": "integer"}],
        )


def test_too_many_parameters_are_refused() -> None:
    params = {f"p{i}": i for i in range(101)}
    template = "select " + ", ".join(f"{{p{i}}}" for i in range(101))
    with pytest.raises(QueryCompileError, match="more than 100"):
        compile_query(template, params, "postgres")


def test_placeholders_are_listed_once_in_first_appearance_order() -> None:
    assert placeholders_of("select {a}, {b}, {a}, {r.start}, {r.end} from t where x = {c_1}") == (
        "a",
        "b",
        "r",
        "c_1",
    )
    assert placeholders_of("select 1") == ()


def test_a_repeated_slot_binds_one_value() -> None:
    bound = compile_query("select {a} where {a} = {a}", {"a": 1}, "postgres")
    assert bound.sql == "select %(a)s where %(a)s = %(a)s" and bound.params == {"a": 1}
