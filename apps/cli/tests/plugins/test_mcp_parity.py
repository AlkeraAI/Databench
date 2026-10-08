"""Cross-backend parity.

Both harness backends connect to ONE parent-hosted loopback MCP server;
the Claude and OpenCode wirings differ only by transport discriminator (``http``
vs ``remote``), pointing at the SAME ``url`` + bearer. The tool surface both serve
derives from the single ``alkera_tool_descriptors`` builder over
``ToolRegistry.hot_prefix`` — the lockstep guarantee. (The earlier stdio
``alkera mcp`` bridge was removed: a child process can't reach the parent's
session broker, so it couldn't gate writes — B14.) The live opencode_e2e /
claude_e2e tiers prove the actual subprocess wiring on top.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import duckdb
from alkera_cli.plugins.plugin_base import PluginRegistry, WorkspaceEvent
from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors
from alkera_core.project.directory import ProjectDirectory

_ROOT = Path(__file__).resolve().parents[4]


def _seed(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()


async def _activated_registry(tmp_path: Path):  # type: ignore[no-untyped-def]
    plugins = PluginRegistry(ProjectDirectory(tmp_path / ".alkera"), tmp_path)
    await plugins.discover()
    await plugins.evaluate_activation(WorkspaceEvent(kind="open", workspace_root=tmp_path))
    plugins.add_all_detected()  # make the discovered duckdb connection live
    return plugins.tool_registry()


async def test_descriptors_are_the_hot_set_both_transports_serve(tmp_path: Path) -> None:
    _seed(tmp_path)
    registry = await _activated_registry(tmp_path)
    hot_names = sorted(s.name for s in registry.hot_prefix())

    # The ONE descriptor builder the loopback server serves to BOTH backends.
    names = sorted(d.name for d in alkera_tool_descriptors(registry))
    assert names == hot_names
    assert {"search_tools", "call_tool"} <= set(hot_names)


async def test_descriptor_schemas_are_deterministic(tmp_path: Path) -> None:
    """The cache-stability invariant: the same registry yields byte-identical
    descriptors on repeated builds, so the served tool list never silently
    re-bills across turns."""
    _seed(tmp_path)
    registry = await _activated_registry(tmp_path)

    def _canon(descriptors: list) -> str:  # type: ignore[type-arg]
        rows = sorted(
            (
                {"name": d.name, "description": d.description, "schema": d.input_schema}
                for d in descriptors
            ),
            key=lambda r: r["name"],
        )
        return json.dumps(rows, sort_keys=True)

    assert _canon(alkera_tool_descriptors(registry)) == _canon(alkera_tool_descriptors(registry))


def test_tool_manifest_is_current_and_generated_consumers_cover_it() -> None:
    """Byte-check the manifest and the generated consumers that cover it."""
    build_in_clean_process = """
import json
import os.path
import runpy
import sys

# Run as a file, the script finds its sibling modules (open_subset) on its own directory.
sys.path.insert(0, os.path.dirname(sys.argv[1]))
generator = runpy.run_path(sys.argv[1])
print(json.dumps(generator["build_schema"](), indent=2, sort_keys=True))
"""
    generated = subprocess.run(
        [
            sys.executable,
            "-c",
            build_in_clean_process,
            str(_ROOT / "scripts/export_tool_manifest.py"),
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    manifest_path = _ROOT / "packages/shared-openapi/tool-manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert manifest_text == generated.stdout

    manifest = json.loads(manifest_text)
    tool_names = set(manifest["x-alkera-tools"])
    definition_names = set(manifest["$defs"])

    generated_manifest = (_ROOT / "packages/chat-model/src/generated/toolManifest.ts").read_text(
        encoding="utf-8"
    )
    name_list = generated_manifest.split("export const ALKERA_TOOL_NAMES = [", 1)[1].split(
        "] as const", 1
    )[0]
    assert set(re.findall(r'^\s+"([^"]+)",$', name_list, flags=re.MULTILINE)) == tool_names

    generated_schemas = (_ROOT / "packages/chat-model/src/generated/toolSchemas.ts").read_text(
        encoding="utf-8"
    )
    emitted_definitions = set(
        re.findall(r"^export (?:interface|type) ([A-Za-z_][A-Za-z0-9_]*)", generated_schemas, re.M)
    )
    assert definition_names <= emitted_definitions


async def test_call_tool_runs_sql_query_through_the_shared_dispatch(tmp_path: Path) -> None:
    """Both transports invoke the SAME ``registry.dispatch`` core under the hood
    (the loopback server's ``call_tool`` handler) — pin that core directly."""
    _seed(tmp_path)
    registry = await _activated_registry(tmp_path)
    result = await registry.dispatch(
        "call_tool",
        {
            "name": "sql.query",
            "args": {"mode": "sql", "connection": "warehouse", "sql": "SELECT 42 AS answer"},
        },
    )
    assert result["result"]["preview_rows"] == [[42]]


def test_alkera_mcp_block_points_both_backends_at_one_server() -> None:
    """One shared server: both backends get the SAME loopback URL + bearer headers; only
    the transport discriminator differs (Claude ``http`` vs OpenCode ``remote``)."""
    from alkera_cli.harness.runtime import _alkera_mcp_block

    url = "http://127.0.0.1:54321/mcp"
    headers = {"Authorization": "Bearer secret-token"}

    claude = _alkera_mcp_block(url, headers, claude=True)["alkera"]
    assert claude["type"] == "http"
    assert claude["url"] == url
    assert claude["headers"] == headers

    opencode = _alkera_mcp_block(url, headers, claude=False)["alkera"]
    assert opencode["type"] == "remote"
    assert opencode["url"] == url
    assert opencode["headers"] == headers
    assert opencode["enabled"] is True


async def test_claude_adapter_registers_the_alkera_server(tmp_path: Path) -> None:
    """The Claude in-process transport exposes the Alkera tool surface IFF a
    ToolRegistry is wired (parity with the OpenCode local-MCP transport). No
    subprocess — we only build the adapter + its MCP-server descriptor."""
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
    from alkera_cli.harness.claude_binary import (
        ClaudeBinaryNotFoundError,
        resolve_claude_binary,
    )
    from alkera_cli.harness.event_bus import EventBus

    try:
        binary = resolve_claude_binary()
    except ClaudeBinaryNotFoundError:
        import pytest

        pytest.skip("no claude binary resolvable")

    _seed(tmp_path)
    registry = await _activated_registry(tmp_path)

    def _adapter(native: dict) -> ClaudeAgentAdapter:  # type: ignore[type-arg]
        config = SessionConfig(
            session_id="11111111-1111-4111-8111-111111111111",
            project_dir=tmp_path,
            chat_dir=tmp_path / "chat",
            harness_native=native,
        )
        (tmp_path / "chat").mkdir(exist_ok=True)
        return ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())

    # With a registry wired → the alkera SDK-MCP server is built.
    assert _adapter({"tool_registry": registry})._make_alkera_server() is not None
    # Without one → no server (e.g. tests / projects with no plugins).
    assert _adapter({})._make_alkera_server() is None


async def test_claude_adapter_routes_to_the_shared_http_server(tmp_path: Path) -> None:
    """One shared server: given the parent-hosted MCP block, the Claude adapter wires it
    into ``mcp_servers["alkera"]`` as an ``http`` server pointing at the SAME
    loopback URL + bearer headers OpenCode uses — one server, both backends."""
    from alkera_cli.harness.adapter import SessionConfig
    from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
    from alkera_cli.harness.claude_binary import (
        ClaudeBinaryNotFoundError,
        resolve_claude_binary,
    )
    from alkera_cli.harness.event_bus import EventBus
    from alkera_cli.harness.runtime import _alkera_mcp_block

    try:
        binary = resolve_claude_binary()
    except ClaudeBinaryNotFoundError:
        import pytest

        pytest.skip("no claude binary resolvable")

    url = "http://127.0.0.1:51234/mcp"
    headers = {"Authorization": "Bearer tok"}
    (tmp_path / "chat").mkdir(exist_ok=True)
    config = SessionConfig(
        session_id="11111111-1111-4111-8111-111111111111",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"alkera_mcp": _alkera_mcp_block(url, headers, claude=True)},
    )
    adapter = ClaudeAgentAdapter(config, binary=binary, event_bus=EventBus())
    options = adapter._build_options(resume=False)
    server = options.mcp_servers["alkera"]
    assert server["type"] == "http"
    assert server["url"] == url
    assert server["headers"] == headers
