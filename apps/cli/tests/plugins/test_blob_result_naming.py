"""The blob-result naming contract: a tool that can return a too-large result
asks the model for a succinct `result_name`, and the spilled-result envelope
carries a name + type so the editor can render a named reference chip.
"""

from __future__ import annotations

import json

from alkera_cli.plugins.plugin_base.delivery import (
    RESULT_INLINE_BYTE_CAP,
    SqlQueryResult,
    deliver_generic,
)
from alkera_cli.plugins.plugin_base.sql_tools import SqlQueryTool
from alkera_core.project.directory import ProjectDirectory


def test_sql_query_input_requires_a_result_name_on_both_signatures() -> None:
    # The model sees `result_name` in the schema for both the raw-SQL and the
    # structured-table call shapes.
    assert "result_name" in json.dumps(SqlQueryTool.input_schema())

    by_sql = SqlQueryTool.validate_args(
        {"mode": "sql", "connection": "c", "sql": "SELECT 1", "result_name": "row count"}
    )
    assert by_sql.root.result_name == "row count"

    by_table = SqlQueryTool.validate_args(
        {"mode": "table", "connection": "c", "table": "t", "result_name": "sample rows"}
    )
    assert by_table.root.result_name == "sample rows"


def test_sql_query_tool_description_asks_the_model_to_name_the_result() -> None:
    assert "result_name" in SqlQueryTool.spec.description


def test_sql_query_result_carries_the_model_provided_name() -> None:
    assert (
        SqlQueryResult(result_name="Q3 revenue by region").model_dump()["result_name"]
        == "Q3 revenue by region"
    )


def test_oversized_spill_envelope_is_named_and_typed(tmp_path) -> None:
    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    big = {"data": "x" * (RESULT_INLINE_BYTE_CAP + 100)}

    out = deliver_generic(blobs, "mytool", big)

    assert out["truncated"] is True
    assert out["name"] == "mytool result"
    assert out["ref_type"] == "text"
    assert isinstance(out["blob"]["sha256"], str)


def test_small_result_is_not_spilled_or_renamed(tmp_path) -> None:
    blobs = ProjectDirectory(tmp_path / ".alkera").blobs()
    small = {"ok": True, "rows": [[1, 2]]}

    assert deliver_generic(blobs, "mytool", small) == small
