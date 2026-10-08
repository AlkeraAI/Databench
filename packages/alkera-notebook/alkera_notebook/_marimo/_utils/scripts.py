# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

# Moved to marimo._environments.script_metadata; re-exported for compatibility.
from alkera_notebook._marimo._environments.script_metadata import (
    REGEX,
    dumps as write_pyproject_to_script,
    loads as read_pyproject_from_script,
    with_python_version_requirement,
    wrap_block as wrap_script_metadata,
)

__all__ = [
    "REGEX",
    "read_pyproject_from_script",
    "with_python_version_requirement",
    "wrap_script_metadata",
    "write_pyproject_to_script",
]
