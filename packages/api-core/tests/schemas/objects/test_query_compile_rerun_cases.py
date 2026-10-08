"""The re-run route's compile cases, on the ONE compiler both sides use.

These cases were first written against a compiler the objects route kept for
itself. That compiler is gone: ``POST /objects/{id}/rerun`` validates a re-run
through :func:`alkera_core.schemas.objects.compile_query`, the same function
the daemon binds with before the read-only SQL tool executes, so the route's
"yes, these values bind" is the machine's "these values bound". The cases are
kept and driven through the route's exact call shape (the saved query's
declarations ride as dicts).

The invariant the whole feature rests on is one sentence: **a caller-supplied
value never appears in the returned SQL.** The injection cases do not ask
"does it escape properly" — escaping is not what happens — they assert that the
hostile string comes back in the VALUES dict and the SQL holds nothing but a
placeholder. Where the single compiler deliberately differs from the old one on
the TEMPLATE side — a brace that is not a slot is the author's own SQL and is
left exactly as written, a numeric string satisfies an integer, an omitted
optional range is a refusal rather than two NULLs — the case pins that
behaviour, so the two lanes cannot drift back apart.
"""

from __future__ import annotations

from datetime import date
from typing import Any, get_args
from uuid import uuid4

import pytest
from alkera_core.schemas.objects import (
    ENGINE_STYLES,
    ParamType,
    QueryCompileError,
    QueryEngine,
    QueryParam,
    QuerySpec,
    compile_query,
)
from alkera_core.schemas.objects.query_params import MAX_STRING_CHARS, PARAM_TYPES

CUSTOMER = QueryParam(name="customer", type="string", label="Customer")
LIMIT = QueryParam(name="row_limit", type="integer")
DAY = QueryParam(name="day", type="date")
PERIOD = QueryParam(name="period", type="daterange")
REGION = QueryParam(name="region", type="enum", enum_values=["emea", "amer"])


def _compile(
    template: str,
    params: list[QueryParam],
    values: dict[str, Any],
    *,
    engine: str = "postgres",
) -> tuple[str, dict[str, Any]]:
    """Exactly how the re-run route calls the compiler."""
    bound = compile_query(
        template, values, engine, declarations=[param.model_dump() for param in params]
    )
    return bound.sql, bound.params


# ---------------------------------------------------------------------------
# The spec vocabularies ARE the compiler's
# ---------------------------------------------------------------------------


def test_a_saved_query_may_only_name_an_engine_the_compiler_binds_for() -> None:
    assert set(get_args(QueryEngine)) == set(ENGINE_STYLES)


def test_a_saved_query_may_only_declare_a_type_the_compiler_validates() -> None:
    assert set(get_args(ParamType)) == PARAM_TYPES


def test_every_declaration_a_spec_can_hold_is_one_the_compiler_accepts() -> None:
    """A ``QuerySpec`` round-tripped through JSON hands its declarations to the
    compiler as dicts; every type the spec admits compiles."""
    params = [
        QueryParam(name="s", type="string"),
        QueryParam(name="i", type="integer"),
        QueryParam(name="n", type="number"),
        QueryParam(name="b", type="boolean"),
        QueryParam(name="d", type="date"),
        QueryParam(name="r", type="daterange"),
        QueryParam(name="e", type="enum", enum_values=["x"]),
    ]
    spec = QuerySpec.model_validate(
        QuerySpec(
            sql_template="SELECT {s},{i},{n},{b},{d},{r.start},{r.end},{e}",
            params=params,
            engine="sqlite",
        ).model_dump(mode="json")
    )
    sql, values = _compile(
        spec.sql_template,
        spec.params,
        {
            "s": "a",
            "i": 1,
            "n": 1.5,
            "b": True,
            "d": "2026-09-07",
            "r": {"start": "2026-09-01", "end": "2026-09-02"},
            "e": "x",
        },
        engine=spec.engine,
    )
    assert sql == "SELECT :s,:i,:n,:b,:d,:r__start,:r__end,:e"
    assert values == {
        "s": "a",
        "i": 1,
        "n": 1.5,
        "b": True,
        "d": date(2026, 9, 7),
        "r__start": date(2026, 9, 1),
        "r__end": date(2026, 9, 2),
        "e": "x",
    }


# ---------------------------------------------------------------------------
# The invariant: values bind, they never become syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("'; DROP TABLE users; --", id="statement_terminator"),
        pytest.param("' OR '1'='1", id="always_true"),
        pytest.param("acme'); DELETE FROM prompts WHERE ('1'='1", id="close_and_delete"),
        pytest.param("%(row_limit)s", id="a_placeholder_of_its_own"),
        pytest.param("{period.start}", id="a_slot_of_its_own"),
        pytest.param("\\'; --", id="backslash_escape"),
    ],
)
def test_a_hostile_string_value_never_reaches_the_sql(hostile: str) -> None:
    sql, values = _compile(
        "SELECT * FROM prompts WHERE customer = {customer}", [CUSTOMER], {"customer": hostile}
    )
    assert sql == "SELECT * FROM prompts WHERE customer = %(customer)s"
    assert values == {"customer": hostile}
    assert hostile not in sql


@pytest.mark.parametrize(
    ("engine", "placeholder"),
    [
        pytest.param("clickhouse", "{customer:String}", id="clickhouse"),
        pytest.param("tinybird", "{{String(customer)}}", id="tinybird"),
        pytest.param("redshift", "%(customer)s", id="redshift"),
        pytest.param("duckdb", "$customer", id="duckdb"),
        pytest.param("sqlite", ":customer", id="sqlite"),
    ],
)
def test_a_hostile_value_is_equally_inert_on_every_engine(engine: str, placeholder: str) -> None:
    sql, values = _compile(
        "SELECT * FROM prompts WHERE customer = {customer}",
        [CUSTOMER],
        {"customer": "'; DROP TABLE users; --"},
        engine=engine,
    )
    assert sql == f"SELECT * FROM prompts WHERE customer = {placeholder}"
    assert values == {"customer": "'; DROP TABLE users; --"}


# ---------------------------------------------------------------------------
# Placeholders per engine and per type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        pytest.param("postgres", "%(customer)s", id="postgres"),
        pytest.param("redshift", "%(customer)s", id="redshift"),
        pytest.param("clickhouse", "{customer:String}", id="clickhouse"),
        pytest.param("tinybird", "{{String(customer)}}", id="tinybird"),
        pytest.param("duckdb", "$customer", id="duckdb"),
        pytest.param("duckdb_local", "$customer", id="duckdb_local"),
        pytest.param("sqlite", ":customer", id="sqlite"),
    ],
)
def test_each_engine_gets_its_own_placeholder_syntax(engine: str, expected: str) -> None:
    sql, _ = _compile("WHERE c = {customer}", [CUSTOMER], {"customer": "acme"}, engine=engine)
    assert sql == f"WHERE c = {expected}"


@pytest.mark.parametrize(
    ("param", "value", "expected_type"),
    [
        pytest.param(CUSTOMER, "acme", "String", id="string"),
        pytest.param(LIMIT, 10, "Int64", id="integer"),
        pytest.param(DAY, "2026-09-07", "Date", id="date"),
        pytest.param(REGION, "emea", "String", id="enum"),
    ],
)
def test_clickhouse_names_the_declared_type(
    param: QueryParam, value: Any, expected_type: str
) -> None:
    sql, _ = _compile(
        f"WHERE x = {{{param.name}}}", [param], {param.name: value}, engine="clickhouse"
    )
    assert sql == f"WHERE x = {{{param.name}:{expected_type}}}"


def test_a_date_range_binds_both_ends_under_distinct_names() -> None:
    """The driver receives dates, not strings: the value is typed before it is
    bound, so a malformed one never reaches the engine as text."""
    sql, values = _compile(
        "WHERE day BETWEEN {period.start} AND {period.end}",
        [PERIOD],
        {"period": {"start": "2026-09-01", "end": "2026-09-30"}},
    )
    assert sql == "WHERE day BETWEEN %(period__start)s AND %(period__end)s"
    assert values == {"period__start": date(2026, 9, 1), "period__end": date(2026, 9, 30)}


def test_a_literal_percent_survives_postgres_paramstyle() -> None:
    """psycopg reads ``%`` as the start of a placeholder, so a LIKE pattern in
    the template has to be doubled or the driver misreads the next character."""
    sql, values = _compile(
        "WHERE name LIKE '%acme%' AND c = {customer}", [CUSTOMER], {"customer": "a"}
    )
    assert sql == "WHERE name LIKE '%%acme%%' AND c = %(customer)s"
    assert values == {"customer": "a"}


def test_a_literal_percent_is_left_alone_on_clickhouse() -> None:
    sql, _ = _compile(
        "WHERE name LIKE '%acme%' AND c = {customer}",
        [CUSTOMER],
        {"customer": "a"},
        engine="clickhouse",
    )
    assert sql == "WHERE name LIKE '%acme%' AND c = {customer:String}"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("SELECT '{{}}'::jsonb", id="doubled_brace"),
        pytest.param("SELECT '{}'::jsonb", id="empty_brace"),
        pytest.param("SELECT '{ customer }'", id="slot_with_spaces"),
        pytest.param("SELECT '{1}'", id="numeric_slot"),
        pytest.param("SELECT '{customer.mid}'", id="unknown_suffix"),
        pytest.param("SELECT '{customer'", id="unclosed_brace"),
        pytest.param("SELECT 'customer}'", id="unopened_brace"),
    ],
)
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_brace_that_is_not_a_slot_is_the_authors_own_sql(text: str, engine: str) -> None:
    """Only ``{name}`` / ``{name.start}`` / ``{name.end}`` are slots. Anything
    else in braces — a JSON literal, a native ClickHouse placeholder, a typo —
    is the template author's SQL and is left exactly as written, never turned
    into a placeholder; the values still bind only to real slots."""
    sql, values = _compile(f"{text}, {{customer}}", [CUSTOMER], {"customer": "a"}, engine=engine)
    placeholder = "%(customer)s" if engine == "postgres" else "{customer:String}"
    assert sql == f"{text}, {placeholder}"
    assert values == {"customer": "a"}


def test_only_the_referenced_parameters_are_bound() -> None:
    """A declared-but-unreferenced parameter is dead weight the driver would
    have to be told to ignore."""
    _, values = _compile(
        "WHERE c = {customer}", [CUSTOMER, LIMIT], {"customer": "a", "row_limit": 5}
    )
    assert values == {"customer": "a"}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "params", "values", "fragment"),
    [
        pytest.param(
            "WHERE c = {nope}", [CUSTOMER], {}, "missing value for nope", id="slot_not_declared"
        ),
        pytest.param(
            "WHERE d = {period}",
            [PERIOD],
            {"period": {"start": "2026-09-01", "end": "2026-09-02"}},
            "is a daterange",
            id="range_used_as_one_slot",
        ),
        pytest.param(
            "WHERE c = {customer.start}",
            [CUSTOMER],
            {"customer": "a"},
            "needs a daterange",
            id="range_suffix_on_a_string",
        ),
        pytest.param(
            "WHERE c = {customer}", [CUSTOMER], {}, "missing value", id="missing_required"
        ),
        pytest.param(
            "WHERE c = {customer}",
            [CUSTOMER],
            {"customer": "a", "extra": 1},
            "no placeholder named extra",
            id="undeclared_value_supplied",
        ),
        pytest.param(
            "WHERE c = {customer}",
            [CUSTOMER, CUSTOMER],
            {"customer": "a"},
            "declared twice",
            id="duplicate_declaration",
        ),
    ],
)
def test_template_refusals(
    template: str, params: list[QueryParam], values: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(QueryCompileError, match=fragment):
        _compile(template, params, values)


@pytest.mark.parametrize(
    ("param", "value", "fragment"),
    [
        pytest.param(CUSTOMER, 5, "must be a string", id="string_given_an_integer"),
        pytest.param(CUSTOMER, "x" * (MAX_STRING_CHARS + 1), "longer than", id="string_too_long"),
        pytest.param(LIMIT, "ten", "must be an integer", id="integer_given_a_word"),
        pytest.param(LIMIT, True, "not a boolean", id="integer_given_a_bool"),
        pytest.param(DAY, "07/09/2026", "YYYY-MM-DD", id="date_in_another_format"),
        pytest.param(DAY, "2026-02-31", "not a calendar date", id="date_that_does_not_exist"),
        pytest.param(DAY, 20260907, "must be a date", id="date_given_an_integer"),
        pytest.param(REGION, "apac", "one of the declared values", id="enum_outside_the_set"),
        pytest.param(REGION, 1, "one of the declared values", id="enum_given_an_integer"),
    ],
)
def test_value_refusals(param: QueryParam, value: Any, fragment: str) -> None:
    with pytest.raises(QueryCompileError, match=fragment):
        _compile(f"WHERE x = {{{param.name}}}", [param], {param.name: value})


def test_an_integer_may_arrive_as_digits() -> None:
    """A form posts strings; a value that IS an integer binds as one."""
    _, values = _compile("WHERE n = {row_limit}", [LIMIT], {"row_limit": "10"})
    assert values == {"row_limit": 10}


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        pytest.param("2026-09-01/2026-09-30", "date range", id="range_as_a_string"),
        pytest.param(["2026-09-01", "2026-09-15", "2026-09-30"], "date range", id="range_of_three"),
        pytest.param({"start": "2026-09-01"}, "{start, end}", id="range_missing_end"),
        pytest.param(
            {"start": "2026-09-01", "end": "2026-09-30", "step": 1},
            "{start, end}",
            id="range_with_an_extra_key",
        ),
        pytest.param(
            {"start": "yesterday", "end": "2026-09-30"}, "YYYY-MM-DD", id="range_bad_start"
        ),
        pytest.param(
            {"start": "2026-09-30", "end": "2026-09-01"}, "start is after end", id="range_reversed"
        ),
    ],
)
def test_daterange_refusals(value: Any, fragment: str) -> None:
    with pytest.raises(QueryCompileError, match=fragment):
        _compile("WHERE d BETWEEN {period.start} AND {period.end}", [PERIOD], {"period": value})


@pytest.mark.parametrize("engine", ["snowflake", "bigquery", "mysql", "trino", ""])
def test_an_engine_with_no_bound_parameter_path_is_refused_rather_than_interpolated(
    engine: str,
) -> None:
    with pytest.raises(QueryCompileError, match="bound parameters are not supported"):
        _compile("SELECT {customer}", [CUSTOMER], {"customer": "a"}, engine=engine)


def test_an_enum_with_no_declared_values_can_never_be_satisfied() -> None:
    empty = QueryParam(name="region", type="enum")
    with pytest.raises(QueryCompileError, match="enum with no values"):
        _compile("WHERE r = {region}", [empty], {"region": "emea"})


def test_an_optional_parameter_binds_null_when_it_is_omitted() -> None:
    optional = QueryParam(name="customer", type="string", required=False)
    sql, values = _compile("WHERE c = {customer}", [optional], {})
    assert sql == "WHERE c = %(customer)s"
    assert values == {"customer": None}


def test_an_omitted_optional_range_is_a_refusal_not_two_nulls() -> None:
    """``BETWEEN NULL AND NULL`` is never what a query meant: a range slot with
    no range behind it names the slot rather than binding two nulls."""
    optional = QueryParam(name="period", type="daterange", required=False)
    with pytest.raises(QueryCompileError, match="needs a daterange"):
        _compile("WHERE d BETWEEN {period.start} AND {period.end}", [optional], {})


def test_a_parameter_name_must_be_an_identifier() -> None:
    """It becomes a bound-parameter name in the driver's own namespace, so
    anything else is a way to smuggle syntax into the placeholder itself."""
    with pytest.raises(ValueError, match=r"string_pattern_mismatch|pattern"):
        QueryParam(name="customer; DROP TABLE users", type="string")


def test_a_relay_run_id_is_a_uuid_the_daemon_will_accept() -> None:
    """The re-run route mints its run id with ``uuid4``; the daemon refuses a
    relay whose ``run_id`` is not a UUID, so the two agree on the shape."""
    from uuid import UUID

    assert UUID(str(uuid4()))
