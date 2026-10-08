"""The one place the format library reaches into the marimo fork.

Everything the reader and writer use from ``alkera_notebook._marimo`` is
imported here, so a fork bump has a single surface to check.
"""

from __future__ import annotations

from alkera_notebook._marimo._ast import parse as marimo_parse
from alkera_notebook._marimo._ast.app_config import _AppConfig as AppConfig
from alkera_notebook._marimo._ast.cell import (
    CellConfig,
    CellOutput,
    CellStaleState,
    ImportWorkspace,
    RunResultStatus,
    RuntimeState,
)
from alkera_notebook._marimo._ast.codegen import (
    INDENT,
    build_setup_section,
    generate_app_constructor,
    pop_setup_cell,
    safe_serialize_cell,
)
from alkera_notebook._marimo._ast.compiler import compile_cell
from alkera_notebook._marimo._ast.names import SETUP_CELL_NAME
from alkera_notebook._marimo._ast.scanner import scan_notebook
from alkera_notebook._marimo._ast.toplevel import TopLevelExtraction
from alkera_notebook._marimo._ast.variables import BUILTINS
from alkera_notebook._marimo._ast.visitor import SQL_CALLS, ScopedVisitor
from alkera_notebook._marimo._dependencies.dependencies import DependencyManager
from alkera_notebook._marimo._types.ids import CellId_t as CellId
from alkera_notebook._marimo._version import __version__ as MARIMO_VERSION  # noqa: N812

__all__ = [
    "BUILTINS",
    "INDENT",
    "MARIMO_VERSION",
    "SETUP_CELL_NAME",
    "SQL_CALLS",
    "AppConfig",
    "CellConfig",
    "CellId",
    "CellOutput",
    "CellStaleState",
    "DependencyManager",
    "ImportWorkspace",
    "RunResultStatus",
    "RuntimeState",
    "ScopedVisitor",
    "TopLevelExtraction",
    "build_setup_section",
    "compile_cell",
    "generate_app_constructor",
    "marimo_parse",
    "pop_setup_cell",
    "safe_serialize_cell",
    "scan_notebook",
]
