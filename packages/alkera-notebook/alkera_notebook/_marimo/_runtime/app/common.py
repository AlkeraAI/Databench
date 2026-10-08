# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from typing import Any

from alkera_notebook._marimo._types.ids import CellId_t

OutputsType = dict[CellId_t, Any]
DefsType = dict[str, Any]

RunOutput = tuple[OutputsType, DefsType]
