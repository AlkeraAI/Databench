# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""
marimo._convert - Format conversion utilities for marimo notebooks.

Provides bidirectional conversion between marimo's internal representation
and various notebook/script formats.
"""

from __future__ import annotations

# Only import the main converters at module level to avoid circular imports.
# Other conversion functions should be imported directly from their submodules:
#   from marimo._convert.markdown import convert_from_ir_to_markdown
#   from marimo._convert.ipynb import convert_from_ipynb_to_notebook_ir
#   etc.
from alkera_notebook._marimo._convert.converters import (
    MarimoConvert,
    MarimoConverterIntermediate,
)

__all__ = [
    "MarimoConvert",
    "MarimoConverterIntermediate",
]
