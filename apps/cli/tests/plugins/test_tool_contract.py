"""Tool contract details: derived JSON-Schema, discriminated-union variants,
boundary validation, and the BM25 tokenizer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from alkera_cli.plugins.plugin_base._bm25 import Bm25Index, tokenize
from alkera_cli.plugins.plugin_base.sql_tools import SqlQueryInput, SqlQueryTool, SqlSchemaTool
from pydantic import ValidationError


def test_sql_query_schema_exposes_both_signatures() -> None:
    schema = SqlQueryTool.input_schema()
    variants = schema.get("oneOf") or schema.get("anyOf")
    assert variants is not None and len(variants) == 2


def test_validate_args_accepts_each_variant() -> None:
    by_sql = SqlQueryTool.validate_args({"mode": "sql", "connection": "c", "sql": "SELECT 1"})
    assert isinstance(by_sql, SqlQueryInput)
    by_table = SqlQueryTool.validate_args({"mode": "table", "connection": "c", "table": "t"})
    assert isinstance(by_table, SqlQueryInput)


def test_validate_args_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        SqlQueryTool.validate_args({"mode": "nonsense", "connection": "c"})


def test_validate_args_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        SqlQueryTool.validate_args({"mode": "sql", "connection": "c"})  # no 'sql'


# The wire schema advertises ``mode`` as optional, so agents compose mode-less calls.
# The tranche-post-build-01 run record has T3-D-02, D-04, and
# D-05 each bouncing off a bare discriminator error on exactly that shape and falling
# back to bash, so their verbatim payloads pin the inference. D-04's stray ``kind``
# key rides along ignored; with no ``table`` cue the call lists.
_T3_D02_SQL = (
    "SELECT table_schema, table_name FROM information_schema.tables "  # pins-source: T3-D-02
    "WHERE table_schema IN ('legacy', 'raw') ORDER BY table_schema, table_name"
)
_T3_D05_SQL = (
    "SELECT table_schema, table_name, table_type "  # pins-source: T3-D-05
    "FROM information_schema.tables WHERE table_schema = 'legacy' ORDER BY table_name;"
)


@pytest.mark.parametrize(
    ("validate", "payload", "expected_mode"),
    [
        pytest.param(
            SqlQueryTool.validate_args,
            {"connection": "warehouse", "sql": _T3_D02_SQL},
            "sql",
            id="query-sql-cue-t3-d02",
        ),
        pytest.param(
            SqlQueryTool.validate_args,
            {"connection": "warehouse", "sql": _T3_D05_SQL},
            "sql",
            id="query-sql-cue-t3-d05",
        ),
        pytest.param(
            SqlQueryTool.validate_args,
            {"connection": "warehouse", "table": "legacy.fct_wide_all"},
            "table",
            id="query-table-cue",
        ),
        pytest.param(
            SqlSchemaTool.validate_args,
            {"connection": "warehouse", "table": "legacy.fct_wide_all"},
            "describe",
            id="schema-table-cue-describes",
        ),
        pytest.param(
            SqlSchemaTool.validate_args,
            {"connection": "warehouse", "kind": "tables"},
            "list",
            id="schema-no-table-lists-t3-d04",
        ),
    ],
)
def test_mode_less_call_resolves_from_its_cue(
    validate: Callable[[dict[str, Any]], Any], payload: dict[str, Any], expected_mode: str
) -> None:
    assert validate(payload).root.mode == expected_mode


def test_mode_less_call_with_no_cue_names_both_legal_shapes() -> None:
    """A payload carrying neither cue is refused with both legal shapes spelled
    out, never the bare discriminator error the tranche agents hit."""
    with pytest.raises(ValidationError) as excinfo:
        SqlQueryTool.validate_args({"connection": "warehouse"})
    message = str(excinfo.value)
    assert "mode='sql'" in message and "'sql' field" in message
    assert "mode='table'" in message and "'table' field" in message
    assert "union_tag_not_found" not in message


@pytest.mark.parametrize(
    ("text", "expected_subset"),
    [
        ("sql.query", {"sql", "query"}),
        ("unload_to_stage", {"unload", "to", "stage"}),
        ("runSQLNow", {"run", "sql", "now"}),
        ("snowflake-prod", {"snowflake", "prod"}),
    ],
)
def test_tokenize_splits_identifiers(text: str, expected_subset: set[str]) -> None:
    assert expected_subset <= set(tokenize(text))


def test_bm25_ranks_relevant_doc_first() -> None:
    index = Bm25Index(
        {
            "a": "fetch rows from a warehouse table",
            "b": "send an email to a teammate",
            "c": "read a local file from disk",
        }
    )
    hits = index.search("warehouse table rows", k=3)
    assert hits[0][0] == "a"
