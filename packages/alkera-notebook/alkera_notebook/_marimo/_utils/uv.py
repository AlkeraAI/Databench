# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

# Moved to marimo._environments.uv; re-exported for compatibility.
from alkera_notebook._marimo._environments.uv import find_uv_bin

__all__ = ["find_uv_bin"]
