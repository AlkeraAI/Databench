"""One slot grammar, driven from the recording that proved there were two.

A real agent does not write ``{customer}``. Asked for a parameterised
deliverable against Tinybird it writes Tinybird's own typed slots —
``{{String(customer_id)}}``, ``{{Date(start_date)}}`` — because that is the
syntax the engine binds, and the consequence was: the
query page rendered "This query takes no parameters." over a statement with
three of them, so "change the customer parameter and re-run" was impossible.

Both spellings are now the same grammar, and both sides read it from one
recorded fixture: this module drives the server's :func:`slots_of` over it and
``apps/web/src/tests/lib/querySlots.test.ts`` drives the browser's ``slotsOf``
over the same bytes, so a spelling one side learns and the other does not is a
red test rather than a form field that never appears.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alkera_core.schemas.objects import QueryCompileError, compile_query
from alkera_core.schemas.objects.query_params import TEMPLATE_SLOT_TYPES, slots_of

REPO_ROOT = Path(__file__).resolve().parents[5]
FIXTURE = REPO_ROOT / "packages/api-core/tests/fixtures/objects/seam/query_slots.json"
CASES: dict[str, Any] = json.loads(FIXTURE.read_text())
RECORDED: dict[str, Any] = CASES["recorded"]


def test_the_fixture_exists_so_this_module_cannot_pass_vacuously() -> None:
    assert FIXTURE.is_file(), f"missing {FIXTURE}"
    assert CASES["grammar"], "the grammar matrix is empty"


# --------------------------------------------------------------------------- #
# the grammar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "case", [pytest.param(case, id=str(case["id"])) for case in CASES["grammar"]]
)
def test_the_slot_reader_finds_exactly_the_slots_the_fixture_names(case: dict[str, Any]) -> None:
    found = [{"name": slot.name, "type": slot.type} for slot in slots_of(str(case["template"]))]
    assert found == case["slots"]


def test_the_matrix_exercises_every_tinybird_type_function() -> None:
    """A type the transport accepts but the matrix never spells is a type one
    side could stop reading without anything going red."""
    spelled = {
        text
        for case in CASES["grammar"]
        for text in TEMPLATE_SLOT_TYPES
        if f"{text}(" in str(case["template"])
    }
    assert spelled == set(TEMPLATE_SLOT_TYPES)


# --------------------------------------------------------------------------- #
# a saved Tinybird statement
# --------------------------------------------------------------------------- #


def test_the_recorded_statement_declares_its_three_parameters() -> None:
    """The whole finding, as one assertion: this statement has parameters."""
    found = [{"name": slot.name, "type": slot.type} for slot in slots_of(RECORDED["sql_template"])]
    assert found == RECORDED["slots"]


def test_the_recorded_statement_compiles_back_to_itself_on_tinybird() -> None:
    """A typed slot is already the engine's own bind syntax, so compiling it
    for Tinybird changes nothing in the text — the values ride beside it. That
    identity is what lets the compiled statement pass the driver's directive
    check, which admits typed slots and nothing else."""
    bound = compile_query(RECORDED["sql_template"], RECORDED["values"], RECORDED["engine"])
    assert bound.sql == RECORDED["sql_template"]
    assert bound.params == {
        "customer_id": "Enterprise",
        "start_date": date(2026, 8, 1),
        "end_date": date(2026, 9, 6),
    }
    for value in RECORDED["values"].values():
        assert str(value) not in bound.sql


def test_the_recorded_statement_binds_a_different_customer_without_moving_the_text() -> None:
    """Change the customer, re-run, get a different answer — and the
    statement that runs is byte-identical, so nothing about the change could
    have been SQL."""
    first = compile_query(RECORDED["sql_template"], RECORDED["values"], RECORDED["engine"])
    changed = {**RECORDED["values"], **RECORDED["changed"]}
    second = compile_query(RECORDED["sql_template"], changed, RECORDED["engine"])
    assert first.sql == second.sql
    assert first.params["customer_id"] != second.params["customer_id"]
    assert second.params["customer_id"] == RECORDED["changed"]["customer_id"]


def test_the_recorded_statement_binds_on_postgres_placeholders_too() -> None:
    """The same template, saved against a Postgres connection, compiles to
    psycopg's placeholder — the grammar is the compiler's, not Tinybird's."""
    bound = compile_query(RECORDED["sql_template"], RECORDED["values"], "postgres")
    assert "{{" not in bound.sql
    assert "%(customer_id)s" in bound.sql
    assert "%(start_date)s" in bound.sql and "%(end_date)s" in bound.sql


def test_a_value_the_reader_types_cannot_become_a_slot() -> None:
    """The property the transport exists for, over the recorded statement."""
    hostile = {
        **RECORDED["values"],
        "customer_id": "{{tb_secret('admin')}}' OR 1=1 --",
    }
    bound = compile_query(RECORDED["sql_template"], hostile, RECORDED["engine"])
    assert bound.sql == RECORDED["sql_template"]
    assert bound.params["customer_id"] == hostile["customer_id"]


# --------------------------------------------------------------------------- #
# the refusal a typed slot must NOT relax
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "directive",
    [
        pytest.param("{{sql_and(a=Int64(x))}}", id="a-directive-that-builds-sql"),
        pytest.param("{{tb_secret('admin')}}", id="a-secret-read"),
        pytest.param("{{columns(cols)}}", id="a-directive-that-writes-a-column-list"),
        pytest.param("{% if x %}1{% end %}", id="a-control-block"),
        pytest.param("{{customer_id}}", id="a-bare-interpolation"),
    ],
)
def test_a_directive_beside_the_typed_slots_is_still_refused(directive: str) -> None:
    """Teaching the compiler Tinybird's slot syntax must not teach it the rest
    of Tinybird's template language: every other ``{{…}}`` / ``{%…%}`` writes
    text INTO the statement instead of binding beside it, and a statement that
    carries one is refused before a value is handed to it."""
    template = f"{RECORDED['sql_template']} /* {directive} */"
    with pytest.raises(QueryCompileError, match="may not contain"):
        compile_query(template, RECORDED["values"], RECORDED["engine"])
