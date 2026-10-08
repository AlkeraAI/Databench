# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import Any

from alkera_notebook._marimo._ai._tools.base import ToolBase
from alkera_notebook._marimo._ai._tools.tools.cells import (
    GetCellOutputs,
    GetCellRuntimeData,
    GetLightweightCellMap,
)
from alkera_notebook._marimo._ai._tools.tools.datasource import GetDatabaseTables
from alkera_notebook._marimo._ai._tools.tools.dependency_graph import GetCellDependencyGraph
from alkera_notebook._marimo._ai._tools.tools.errors import GetNotebookErrors
from alkera_notebook._marimo._ai._tools.tools.lint import LintNotebook
from alkera_notebook._marimo._ai._tools.tools.notebooks import GetActiveNotebooks
from alkera_notebook._marimo._ai._tools.tools.rules import GetMarimoRules
from alkera_notebook._marimo._ai._tools.tools.tables_and_variables import GetTablesAndVariables

SUPPORTED_BACKEND_AND_MCP_TOOLS: list[type[ToolBase[Any, Any]]] = [
    GetMarimoRules,
    GetActiveNotebooks,
    GetCellRuntimeData,
    GetCellOutputs,
    GetLightweightCellMap,
    GetTablesAndVariables,
    GetDatabaseTables,
    GetNotebookErrors,
    LintNotebook,
    GetCellDependencyGraph,
]
