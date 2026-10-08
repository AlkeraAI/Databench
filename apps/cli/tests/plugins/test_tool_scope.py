"""Per-subagent tool restriction — ``tool_scope`` filtering (the subagent
permission spine). An ``explore`` agent sees only its scoped tools in the
advertised list AND is refused out-of-scope tools at dispatch (defense in depth),
so hiding a tool can't be end-run by calling it by name.
"""

from __future__ import annotations

from pathlib import Path

from alkera_cli.plugins.plugin_base import Effect, ToolRegistry
from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_cli.plugins.plugin_base.tool import ToolSpec, tool_in_scope
from alkera_core.project.directory import ProjectDirectory


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    register_sql_tools(registry)
    return registry


def _spec(name: str, effect: Effect) -> ToolSpec:
    return ToolSpec(name=name, hot=True, effect_hint=effect)


def test_tool_in_scope_matrix() -> None:
    read = _spec("read.tool", Effect.READ)
    write = _spec("write.tool", Effect.WRITE)
    # None / [] → unrestricted
    assert tool_in_scope(read, None)
    assert tool_in_scope(write, None)
    assert tool_in_scope(write, [])
    # read_only → only READ-hint tools survive
    assert tool_in_scope(read, "read_only")
    assert not tool_in_scope(write, "read_only")
    # allowlist → exactly the named tools
    assert tool_in_scope(read, ["read.tool"])
    assert not tool_in_scope(read, ["other.tool"])


def test_descriptors_allowlist_filters(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    full = {d.name for d in alkera_tool_descriptors(registry)}
    # The advertised hot set includes the meta tools + the hot sql tool.
    assert {"search_tools", "call_tool", "sql.connections"} <= full
    scoped = {d.name for d in alkera_tool_descriptors(registry, ["sql.connections"])}
    assert scoped == {"sql.connections"}


async def test_dispatch_refuses_out_of_scope(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # In scope → normal dispatch (a missing connection errors, but NOT a scope error).
    out = await registry.dispatch("sql.connections", {}, tool_scope=["sql.connections"])
    assert "not available to this agent" not in out.get("error", "")
    # Out of scope → refused cleanly, before the tool body runs (sql.schema is a
    # registered long-tail tool, reachable via dispatch but not in this scope).
    blocked = await registry.dispatch("sql.schema", {}, tool_scope=["sql.connections"])
    assert "not available to this agent" in blocked["error"]


async def test_unrestricted_dispatch_unaffected(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("sql.connections", {})  # tool_scope defaults None
    assert "not available to this agent" not in out.get("error", "")
