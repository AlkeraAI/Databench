# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Write to a cell's output area."""

__all__ = [
    "append",
    "clear",
    "clear_console",
    "replace",
    "replace_at_index",
]

from alkera_notebook._marimo._runtime.output._output import (
    append,
    clear,
    clear_console,
    replace,
    replace_at_index,
)
