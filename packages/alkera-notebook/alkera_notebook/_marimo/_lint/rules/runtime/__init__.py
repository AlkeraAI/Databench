# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._lint.rules.base import LintRule
from alkera_notebook._marimo._lint.rules.runtime.branch_expression import BranchExpressionRule
from alkera_notebook._marimo._lint.rules.runtime.private_import_alias import (
    PrivateImportAliasRule,
)
from alkera_notebook._marimo._lint.rules.runtime.reusable_definition_order import (
    ReusableDefinitionOrderRule,
)
from alkera_notebook._marimo._lint.rules.runtime.self_import import SelfImportRule

RUNTIME_RULE_CODES: dict[str, type[LintRule]] = {
    "MR001": SelfImportRule,
    "MR002": BranchExpressionRule,
    "MR003": ReusableDefinitionOrderRule,
    "MR004": PrivateImportAliasRule,
}

__all__ = [
    "RUNTIME_RULE_CODES",
    "BranchExpressionRule",
    "PrivateImportAliasRule",
    "ReusableDefinitionOrderRule",
    "SelfImportRule",
]
