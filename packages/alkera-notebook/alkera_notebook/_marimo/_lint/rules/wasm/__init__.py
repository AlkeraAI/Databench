# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

from alkera_notebook._marimo._lint.rules.base import LintRule
from alkera_notebook._marimo._lint.rules.wasm.incompatible_imports import (
    IncompatibleImportsRule,
)
from alkera_notebook._marimo._lint.rules.wasm.incompatible_packages import (
    IncompatiblePackagesRule,
)
from alkera_notebook._marimo._lint.rules.wasm.unsafe_system_calls import UnsafeSystemCallsRule

WASM_RULE_CODES: dict[str, type[LintRule]] = {
    "MW001": IncompatibleImportsRule,
    "MW002": UnsafeSystemCallsRule,
    "MW003": IncompatiblePackagesRule,
}

__all__ = [
    "WASM_RULE_CODES",
    "IncompatibleImportsRule",
    "IncompatiblePackagesRule",
    "UnsafeSystemCallsRule",
]
