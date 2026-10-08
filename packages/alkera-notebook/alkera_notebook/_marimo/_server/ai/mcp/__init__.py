# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""MCP (Model Context Protocol) client implementation for marimo."""

from __future__ import annotations

from alkera_notebook._marimo._server.ai.mcp.client import (
    MCPClient,
    MCPServerConnection,
    MCPServerStatus,
    get_mcp_client,
)
from alkera_notebook._marimo._server.ai.mcp.config import (
    MCP_PRESETS,
    MCPConfigComparator,
    MCPConfigDiff,
    MCPServerDefinition,
    MCPServerDefinitionFactory,
    append_presets,
)
from alkera_notebook._marimo._server.ai.mcp.transport import (
    MCPTransportConnector,
    MCPTransportRegistry,
    MCPTransportType,
    StdioTransportConnector,
    StreamableHTTPTransportConnector,
)
from alkera_notebook._marimo._server.ai.mcp.types import MCPToolArgs

__all__ = [  # noqa: RUF022
    # Client classes
    "MCPClient",
    "MCPServerConnection",
    "MCPServerStatus",
    "get_mcp_client",
    # Config classes
    "MCP_PRESETS",
    "MCPConfigComparator",
    "MCPConfigDiff",
    "MCPServerDefinition",
    "MCPServerDefinitionFactory",
    "append_presets",
    # Transport classes
    "MCPTransportConnector",
    "MCPTransportRegistry",
    "MCPTransportType",
    "StdioTransportConnector",
    "StreamableHTTPTransportConnector",
    # Types
    "MCPToolArgs",
]
