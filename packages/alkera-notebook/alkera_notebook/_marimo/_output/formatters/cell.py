# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._ast.cell import Cell
from alkera_notebook._marimo._messaging.mimetypes import KnownMimeType
from alkera_notebook._marimo._output import formatting
from alkera_notebook._marimo._output.formatters.formatter_factory import FormatterFactory


class CellFormatter(FormatterFactory):
    @staticmethod
    def package_name() -> None:
        return None

    def register(self) -> None:
        @formatting.formatter(Cell)
        def _format_cell(cell: Cell) -> tuple[KnownMimeType, str]:
            return cell._help()._mime_()
