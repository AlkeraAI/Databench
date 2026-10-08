# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._cli.errors import MarimoCLIMissingDependencyError
from alkera_notebook._marimo._dependencies.dependencies import DependencyManager


def require_mcp_dependencies() -> None:
    dependency = DependencyManager.mcp
    if dependency.has_required_version(quiet=True):
        return

    if dependency.has(quiet=True):
        installed_version = dependency.get_version() or "unknown"
        message = (
            f"MCP SDK {installed_version} is not supported. "
            "marimo requires MCP >=2.0.0,<3.0.0."
        )
    else:
        message = "MCP dependencies not available."

    raise MarimoCLIMissingDependencyError(message, "marimo[mcp]")
