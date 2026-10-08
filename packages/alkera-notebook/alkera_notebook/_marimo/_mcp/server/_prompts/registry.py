# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Registry of all supported MCP prompts."""

from alkera_notebook._marimo._mcp.server._prompts.base import PromptBase
from alkera_notebook._marimo._mcp.server._prompts.prompts.errors import ErrorsSummary
from alkera_notebook._marimo._mcp.server._prompts.prompts.notebooks import ActiveNotebooks

SUPPORTED_MCP_PROMPTS: list[type[PromptBase]] = [
    ActiveNotebooks,
    ErrorsSummary,
]
