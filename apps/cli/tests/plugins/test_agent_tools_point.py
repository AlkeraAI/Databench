"""The ``AGENT_TOOLS`` extension point: every tool family beyond the platform's
own reaches the agent by registering, never by the registry naming it.

The CLI suite runs with the product installed, so the platform-alone case is
proven in a fresh interpreter that installs nothing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.agent_tools import (
    ToolBuild,
    ToolServices,
    combined_services,
)
from alkera_cli.plugins.plugin_base.capabilities import CapabilitySet
from alkera_cli.plugins.plugin_base.permissions.wiring import NO_MEASURE_REASON, the_measure
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.tool import ToolError, ToolRegistry, classify_tool_failure
from alkera_core.extensions import ExtensionError
from alkera_core.project import ProjectDirectory

#: The platform's own tools: what an install with no tool source serves.
PLATFORM_TOOLS = {
    "background_cancel",
    "background_status",
    "blob.create",
    "blob.delete",
    "blob.derive",
    "blob.info",
    "blob.materialize",
    "blob.profile",
    "blob.query",
    "call_tool",
    "fetch_result",
    "list_agent_types",
    "list_plugins",
    "manage_tasks",
    "search_tools",
    "spawn_agent",
    "web.fetch",
    "web.search",
}
#: The parent-hosted shell and the environment tools ride on it (POSIX only).
SHELL_TOOLS = {"bash", "environment.capture", "environment.recreate"}


def _platform_tools() -> set[str]:
    return PLATFORM_TOOLS | (SHELL_TOOLS if os.name == "posix" else set())


class _Source:
    def __init__(self, name: str, services: ToolServices) -> None:
        self.name = name
        self._services = services

    def services(self, build: ToolBuild) -> ToolServices:
        return self._services

    def register(self, registry: ToolRegistry, build: ToolBuild) -> None:
        return None


def _build(tmp_path: Path, capabilities: tuple[CapabilitySet | None, ...] = ()) -> ToolBuild:
    project = ProjectDirectory(tmp_path / ".alkera")
    return ToolBuild(
        project=project,
        plugins=PluginRegistry(project, tmp_path),
        capabilities=capabilities,
    )


def test_each_store_comes_from_the_source_that_provides_it(tmp_path: Path) -> None:
    knowledge, ledger = object(), object()
    services = combined_services(
        [
            _Source("knowledge", ToolServices(context_store=knowledge)),
            _Source("metering", ToolServices(cost_ledger=ledger)),
        ],
        _build(tmp_path),
    )

    assert services.context_store is knowledge
    assert services.cost_ledger is ledger
    assert services.lineage_store is None
    assert services.embedder is None
    assert services.knowledge_reader is None


def test_two_sources_providing_one_store_is_refused(tmp_path: Path) -> None:
    sources = [
        _Source("first", ToolServices(lineage_store=object())),
        _Source("second", ToolServices(lineage_store=object())),
    ]

    with pytest.raises(ExtensionError, match="'first' and 'second' both provide lineage_store"):
        combined_services(sources, _build(tmp_path))


def test_no_source_leaves_every_store_unset(tmp_path: Path) -> None:
    assert combined_services([], _build(tmp_path)) == ToolServices()


def test_the_gate_takes_at_most_one_impact_measure() -> None:
    first, second = object(), object()

    assert the_measure([]) is None
    assert the_measure([first]) is first
    with pytest.raises(ExtensionError, match="2 impact measures are registered"):
        the_measure([first, second])


def _raised(error: BaseException, cause: BaseException | None = None) -> BaseException:
    try:
        raise error from cause
    except BaseException as caught:
        return caught


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(ToolError("no", classification="permission"), "permission", id="explicit"),
        pytest.param(
            _raised(ToolError("wrapped", classification="timeout"), ValueError("x")),
            "timeout",
            id="explicit-wins-over-its-cause",
        ),
        pytest.param(
            _raised(ToolError("wrapped"), ToolError("inner", classification="unreachable")),
            "unreachable",
            id="unclassified-reads-its-cause",
        ),
        pytest.param(ToolError("permission denied"), "error", id="never-its-own-sentence"),
    ],
)
def test_a_tool_error_classifies_by_its_field_then_its_cause(
    error: BaseException, expected: str
) -> None:
    assert classify_tool_failure(error) == expected


_PLATFORM_ALONE = """
import asyncio, json, sys
from pathlib import Path
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_core.extensions import installed_extensions
from alkera_core.project import ProjectDirectory
from alkera_cli.plugins.plugin_base.registry import PluginRegistry
from alkera_cli.plugins.plugin_base.permissions.wiring import fs_write_evidence, sql_gate_impact
from alkera_cli.plugins.plugin_base.tool import classify_tool_failure

PLUGINS, FRAMEWORK = "alkera_cli.plugins.", "alkera_cli.plugins.plugin_base."
root = Path(sys.argv[1])
drop = ActionDescriptor(capability="sql", operation="drop_table", effect=Effect.DESTROY)
edit = ActionDescriptor(capability="fs", effect=Effect.WRITE)
tools = PluginRegistry(ProjectDirectory(root / ".alkera"), root).tool_registry(
    web_search_enabled=True
)
print("REPORT" + json.dumps({
    "installed": list(installed_extensions()),
    "tools": sorted(spec.name for spec in tools.all_specs()),
    "stores": [
        type(store).__name__
        for store in (tools.context_store, tools.lineage_store, tools.cost_ledger)
        if store is not None
    ],
    "data_tool_modules": sorted(
        m
        for m in sys.modules
        if m.startswith(PLUGINS) and not m.startswith(FRAMEWORK) and m.endswith("_tool")
    ),
    "timeout_failure": classify_tool_failure(TimeoutError("the host did not answer")),
    "sql_impact": asyncio.run(sql_gate_impact(drop, lineage_store=object())).model_dump(
        mode="json", include={"status", "reason"}
    ),
    "fs_evidence": asyncio.run(
        fs_write_evidence(edit, registry=tools, workspace_root=root)
    ) is None,
}))
"""


def test_the_platform_alone_serves_only_its_own_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_PLATFORM_ALONE), str(workspace)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ALKERA_HOME": str(tmp_path / "home"), "NO_COLOR": "1"},
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    report = json.loads(line.removeprefix("REPORT"))

    assert report["installed"] == []
    assert set(report["tools"]) == _platform_tools()
    assert report["stores"] == []
    # No plugin's tool module is even loaded; the framework's own may be.
    assert report["data_tool_modules"] == []
    # Nobody registered a classifier, so a failure is an error, whatever its text says.
    assert report["timeout_failure"] == "error"
    # Nothing measures impact, and the gate says so rather than reporting no impact.
    assert report["sql_impact"] == {"status": "degraded", "reason": NO_MEASURE_REASON}
    assert report["fs_evidence"] is True
