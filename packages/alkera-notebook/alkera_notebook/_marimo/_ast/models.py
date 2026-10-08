# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from dataclasses import dataclass

from alkera_notebook._marimo._ast.cell import Cell, CellConfig
from alkera_notebook._marimo._types.ids import CellId_t


@dataclass
class CellData:
    """A cell together with its metadata.

    This class bundles a cell with its associated metadata like ID, code, name and config.
    It represents both valid cells that can be executed and invalid/unparsable cells.

    Attributes:
        cell_id: Unique identifier for the cell
        code: Raw source code text of the cell
        name: User-provided name for the cell, or a default if none provided
        config: Configuration options for the cell like column placement, disabled state, etc.
        cell: The compiled Cell object if code is valid, None if code couldn't be parsed
    """

    cell_id: CellId_t
    code: str
    name: str
    config: CellConfig
    cell: Cell | None
