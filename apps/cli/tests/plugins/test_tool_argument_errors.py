"""A tool called with bad arguments is told what to fix, in plain sentences.

The validation failure used to reach the model as pydantic's own dump — the
model's name, every input value echoed back and a documentation link per
problem. The message is now one sentence per problem: the field path and what
is wrong with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base import CapabilitySet, Connection, Environment
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolRegistry, ToolSpec
from alkera_core.project.directory import ProjectDirectory
from pydantic import BaseModel


class _PageInput(BaseModel):
    table: str
    limit: int = 10
    columns: list[str] = []


class _PageOutput(BaseModel):
    ok: bool = True


class _PageTool(Tool[_PageInput, _PageOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="demo.page", title="Page", description="Page a table.", effect_hint=Effect.READ
    )
    Input: ClassVar[type[BaseModel]] = _PageInput
    Output: ClassVar[type[BaseModel]] = _PageOutput

    async def run(self, args: _PageInput, ctx: ToolContext) -> _PageOutput:
        return _PageOutput()


def _registry(tmp_path: Path) -> ToolRegistry:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    registry.register(_PageTool)
    return registry


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param({}, "demo.page needs `table`.", id="a-missing-field"),
        pytest.param(
            {"table": "t", "limit": "ten"},
            "`limit` must be an integer, not 'ten'.",
            id="a-wrong-type",
        ),
        pytest.param(
            {"limit": "ten"},
            "demo.page needs `table`. `limit` must be an integer, not 'ten'.",
            id="two-problems-at-once",
        ),
        pytest.param(
            {"table": "t", "columns": ["a", 7]},
            "`columns.1` must be a string, not 7.",
            id="a-nested-path",
        ),
        pytest.param(
            {"table": {"secret": "x" * 500}},
            "`table` must be a string, not an object.",
            id="a-large-input-is-named-not-echoed",
        ),
    ],
)
async def test_bad_arguments_read_as_one_sentence_per_problem(
    tmp_path: Path, args: dict[str, Any], expected: str
) -> None:
    out = await _registry(tmp_path).dispatch("demo.page", args)

    assert out["error"] == expected
    assert "pydantic" not in out["error"]
    assert "http" not in out["error"]
    assert "input_value" not in out["error"]


def _sql_registry(tmp_path: Path) -> ToolRegistry:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    registry.register_connection(
        Connection(handle="pg", plugin="postgres", dialect="postgres", environment=Environment.DEV),
        capabilities=CapabilitySet(),
    )
    register_sql_tools(registry)
    return registry


async def test_sql_schema_describe_without_a_table_states_the_call_to_send(
    tmp_path: Path,
) -> None:
    """The prod loop: ``sql.schema`` in describe mode with no table. The message
    used to read "needs `describe.table`", which names a nested object the tool
    does not read, so the model rebuilt the same failing call every turn. It now
    names the top-level field and the exact call, on the caller's connection."""
    out = await _sql_registry(tmp_path).dispatch(
        "sql.schema", {"connection": "pg", "mode": "describe", "table_nme": "orders"}
    )

    assert out["error"] == (
        "sql.schema arguments are invalid: mode='describe' needs `table`, the table to "
        "describe, as a top-level string. Send "
        '{"connection": "pg", "mode": "describe", "table": "public.orders"}, or '
        "mode='list' to see the tables. It does not read `table_nme`."
    )


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        pytest.param(
            "sql.query",
            {"connection": "pg", "mode": "sql"},
            "sql.query needs `sql`.",
            id="a-missing-field-is-not-prefixed-with-the-variant-tag",
        ),
        pytest.param(
            "sql.query",
            {"connection": "pg", "mode": "table", "table": "t", "limit": "ten"},
            "`limit` must be an integer, not 'ten'.",
            id="a-wrong-type-is-not-prefixed-with-the-variant-tag",
        ),
        pytest.param(
            "sql.schema",
            {"connection": "pg", "mode": "columns"},
            "`mode` must be 'list' or 'describe', not 'columns'.",
            id="an-unknown-mode-names-the-legal-ones",
        ),
    ],
)
async def test_a_union_input_names_the_fields_the_caller_sends(
    tmp_path: Path, tool: str, args: dict[str, Any], expected: str
) -> None:
    """A tool whose Input is a tagged union reports paths as the caller spells
    them: the variant's tag is not a field, so it never opens a path."""
    out = await _sql_registry(tmp_path).dispatch(tool, args)

    assert out["error"] == expected
