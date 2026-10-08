"""SkillProvider consumption: a plugin's SkillDef is loadable
by the agent via the `use_skill` tool (list → load), end to end through the
registry, NOT a contract-only stub."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base import (
    ActivationSpec,
    Plugin,
    PluginManifest,
    PluginRegistry,
    Registrar,
    SkillDef,
    SurfaceKind,
    ToolRegistry,
    WorkspaceEvent,
)
from alkera_cli.plugins.plugin_base.skill_tool import register_skill_tools
from alkera_core.project.directory import ProjectDirectory


class _SkillsPlugin(Plugin):
    manifest = PluginManifest(name="skilled", surfaces=frozenset({SurfaceKind.SKILL}))

    def register(self, r: Registrar) -> None:
        r.skills(
            [
                SkillDef(
                    name="sql_style",
                    description="House SQL conventions",
                    body="Always qualify columns; never SELECT *.",
                ),
                SkillDef(name="naming", description="Naming rules", body="snake_case everywhere."),
            ]
        )

    def activation(self) -> ActivationSpec:
        return ActivationSpec(always=True)


async def _registry(tmp_path: Path) -> ToolRegistry:
    plugins = PluginRegistry(ProjectDirectory(tmp_path / ".alkera"), tmp_path)
    await plugins.discover(extra_plugins=[_SkillsPlugin])
    await plugins.evaluate_activation(WorkspaceEvent(kind="open", workspace_root=tmp_path))
    return plugins.tool_registry()


async def test_use_skill_is_registered_when_skills_exist(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    assert "use_skill" in [s.name for s in registry.all_specs()]


async def test_use_skill_lists_then_loads(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)

    # LIST (no name) — the agent discovers what's available.
    listed = await registry.dispatch("use_skill", {})
    names = {s["name"]: s["description"] for s in listed["skills"]}
    assert names == {"sql_style": "House SQL conventions", "naming": "Naming rules"}
    assert listed["body"] is None

    # LOAD by name — the full body comes into context.
    loaded = await registry.dispatch("use_skill", {"name": "sql_style"})
    assert loaded["name"] == "sql_style"
    assert loaded["body"] == "Always qualify columns; never SELECT *."


async def test_use_skill_unknown_name_errors_with_catalog(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    out = await registry.dispatch("use_skill", {"name": "nope"})
    assert "unknown skill" in out["error"]
    assert "sql_style" in out["error"]  # the error lists what IS available


def test_no_skill_tool_when_no_skills(tmp_path: Path) -> None:
    # A registry with no skills doesn't surface a useless tool.
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_skill_tools(registry)
    assert "use_skill" not in [s.name for s in registry.all_specs()]


async def test_skill_reachable_via_call_tool(tmp_path: Path) -> None:
    """The long-tail `use_skill` is reachable through the generic call_tool
    dispatcher (the only way a non-hot tool runs)."""
    registry = await _registry(tmp_path)
    out = await registry.dispatch("call_tool", {"name": "use_skill", "args": {"name": "naming"}})
    assert out["result"]["body"] == "snake_case everywhere."


@pytest.mark.usefixtures("tmp_path")
def test_skilldef_is_versioned() -> None:
    assert SkillDef(name="x").schema_version == "1.0.0"
