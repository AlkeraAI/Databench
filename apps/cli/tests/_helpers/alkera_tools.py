"""Shared e2e helper: start the parent-hosted Alkera MCP server (Option A) over a
real activated plugin registry, so a spawned harness subprocess (opencode/claude)
connects to the SAME server both backends use in production.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from alkera_cli.harness.mcp_server import AlkeraToolServer, SessionToolBinding


@contextlib.asynccontextmanager
async def alkera_tool_server(
    workspace_root: Path,
    *,
    broker: Any = None,
    permission_mode: str = "default",
    spawn: Any = None,
) -> AsyncIterator[AlkeraToolServer]:
    """Discover+activate plugins for ``workspace_root`` and serve their tool
    surface over a loopback Streamable-HTTP MCP server, bound to a session
    context (broker + mode + optional spawn). Yields the started server; stops it
    on exit."""
    from alkera_cli.plugins.plugin_base import PluginRegistry, WorkspaceEvent
    from alkera_core.project.directory import ProjectDirectory

    plugins = PluginRegistry(ProjectDirectory(workspace_root / ".alkera"), workspace_root)
    await plugins.discover()
    await plugins.evaluate_activation(WorkspaceEvent(kind="open", workspace_root=workspace_root))
    plugins.add_all_detected()  # e2e: make detected connections live (editor adds explicitly)
    binding = SessionToolBinding(
        registry=plugins.tool_registry(),
        broker=broker,
        permission_mode=permission_mode,
        session_id="e2e",
        spawn=spawn,
    )
    server = AlkeraToolServer(binding)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
