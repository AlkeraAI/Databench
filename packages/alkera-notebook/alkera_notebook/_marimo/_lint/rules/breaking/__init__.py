# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._lint.rules.base import LintRule
from alkera_notebook._marimo._lint.rules.breaking.graph import (
    CycleDependenciesRule,
    MultipleDefinitionsRule,
    SetupCellDependenciesRule,
)
from alkera_notebook._marimo._lint.rules.breaking.syntax_error import SyntaxErrorRule
from alkera_notebook._marimo._lint.rules.breaking.unparsable import UnparsableRule

BREAKING_RULE_CODES: dict[str, type[LintRule]] = {
    "MB001": UnparsableRule,
    "MB002": MultipleDefinitionsRule,
    "MB003": CycleDependenciesRule,
    "MB004": SetupCellDependenciesRule,
    "MB005": SyntaxErrorRule,
}

__all__ = [
    "BREAKING_RULE_CODES",
    "CycleDependenciesRule",
    "MultipleDefinitionsRule",
    "SetupCellDependenciesRule",
    "SyntaxErrorRule",
    "UnparsableRule",
]
