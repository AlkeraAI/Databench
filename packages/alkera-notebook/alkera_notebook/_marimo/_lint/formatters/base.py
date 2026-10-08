# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
"""Base formatter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alkera_notebook._marimo._lint.diagnostic import Diagnostic


class DiagnosticFormatter(ABC):
    """Abstract base class for formatting diagnostics."""

    @abstractmethod
    def format(
        self,
        diagnostic: Diagnostic,
        filename: str,
        code_lines: list[str] | None = None,
    ) -> str:
        """Format a diagnostic for display."""
