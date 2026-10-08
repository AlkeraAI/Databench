"""The ``.alknb.py`` notebook format.

``read`` and ``write`` convert between file text and :class:`NotebookIR`;
``classify`` and ``render_cell`` convert between a cell's code and the text an
editor shows for SQL and Markdown cells; ``analyze`` and ``analyze_code`` give
the dependency graph; ``compile_step`` prepares a cell for the kernel;
``new_cell_id`` mints ids for new cells; ``file_code`` is a cell's code as
the file will hold it (no leading or trailing blank lines). Every function is pure and never
raises on any input; problems are returned as violations.

This is format 1.0, cell ids included.
"""

from __future__ import annotations

from alkera_notebook.format import op_rules
from alkera_notebook.format.graph import analyze, analyze_code, compile_step
from alkera_notebook.format.header import FENCE_CLOSE, FENCE_OPEN, SETTINGS
from alkera_notebook.format.ids import (
    ALPHABET,
    ID_RE,
    SIMILARITY_THRESHOLD,
    is_cell_id,
    mint_id,
    new_cell_id,
    normalize_code,
    resolve,
)
from alkera_notebook.format.ir import (
    FORMAT_VERSION,
    KINDS,
    VIOLATION_CODES,
    CellGraph,
    CellIR,
    GraphError,
    GraphJSON,
    Kind,
    NotebookIR,
    Resolution,
    Violation,
    file_settings,
)
from alkera_notebook.format.migrations import MIGRATIONS
from alkera_notebook.format.reader import read
from alkera_notebook.format.settings import CELL_SETTINGS, NOTEBOOK_SETTINGS, SettingSpec
from alkera_notebook.format.templates import (
    RUNTIME_IMPORT,
    RUNTIME_MODULE,
    classify,
    render_cell,
    setup_with_runtime,
)
from alkera_notebook.format.writer import file_code, write

__all__ = [
    "ALPHABET",
    "CELL_SETTINGS",
    "FENCE_CLOSE",
    "FENCE_OPEN",
    "FORMAT_VERSION",
    "ID_RE",
    "KINDS",
    "MIGRATIONS",
    "NOTEBOOK_SETTINGS",
    "RUNTIME_IMPORT",
    "RUNTIME_MODULE",
    "SETTINGS",
    "SIMILARITY_THRESHOLD",
    "VIOLATION_CODES",
    "CellGraph",
    "CellIR",
    "GraphError",
    "GraphJSON",
    "Kind",
    "NotebookIR",
    "Resolution",
    "SettingSpec",
    "Violation",
    "analyze",
    "analyze_code",
    "classify",
    "compile_step",
    "file_code",
    "file_settings",
    "is_cell_id",
    "mint_id",
    "new_cell_id",
    "normalize_code",
    "op_rules",
    "read",
    "render_cell",
    "resolve",
    "setup_with_runtime",
    "write",
]
