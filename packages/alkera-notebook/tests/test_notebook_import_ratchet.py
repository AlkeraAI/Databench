"""Import ratchets for the notebook open core.

- ``alkera`` and ``_alkera_kernel`` import only the standard library and
  themselves: ``alkera`` runs in the person's environment and the kernel is
  loaded into whatever interpreter that environment provides.
- ``alkera_notebook`` never imports the platform (every other first-party
  package in the tree: ``alkera_core``, ``alkera_cli``, ``backend`` and the
  rest) or ``loro``, so it stays installable and usable on a laptop.

The scan is over source (AST), so an import inside a function, under
``TYPE_CHECKING`` or behind a ``try`` counts the same as a top-level one, and a
constant-string ``importlib.import_module`` / ``__import__`` counts too.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2]

STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}
#: The notebook's own packages; every other first-party package is the platform.
NOTEBOOK_PACKAGES = frozenset({"alkera", "_alkera_kernel", "alkera_notebook"})


def first_party_packages(repo: Path = PACKAGES.parent) -> frozenset[str]:
    """Every top-level package an app or a package of the tree ships."""
    inits = [*repo.glob("apps/*/*/__init__.py"), *repo.glob("packages/*/*/__init__.py")]
    return frozenset(init.parent.name for init in inits)


PLATFORM = (first_party_packages() - NOTEBOOK_PACKAGES) | {"loro"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    module: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} imports {self.module}"


def _imported_modules(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            # A relative import stays inside the package by construction.
            if node.level == 0 and node.module:
                yield node.lineno, node.module
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            first = node.args[0]
            if (
                called in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and not first.value.startswith(".")
            ):
                yield node.lineno, first.value


# The private copy of the marimo fork is generated third-party code (its server
# imports loro); what the format library pulls in from it at run time is pinned
# by test_nbfmt_ratchet.py instead.
GENERATED = frozenset({"_marimo"})


def scan(root: Path, allowed: Callable[[str], bool]) -> list[Violation]:
    """Every import under ``root`` whose top-level module ``allowed`` refuses."""
    found: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if GENERATED & set(path.relative_to(root).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, module in _imported_modules(tree):
            if not allowed(module.partition(".")[0]):
                found.append(Violation(path, line, module))
    return found


def stdlib_and(own: str) -> Callable[[str], bool]:
    return lambda top: top in STDLIB or top == own


def not_platform(top: str) -> bool:
    return top not in PLATFORM


RATCHETS = [
    pytest.param(PACKAGES / "alkera-py" / "alkera", stdlib_and("alkera"), id="alkera"),
    pytest.param(
        PACKAGES / "alkera-kernel" / "_alkera_kernel",
        stdlib_and("_alkera_kernel"),
        id="_alkera_kernel",
    ),
    pytest.param(
        PACKAGES / "alkera-notebook" / "alkera_notebook", not_platform, id="alkera_notebook"
    ),
]


@pytest.mark.parametrize(("root", "allowed"), RATCHETS)
def test_package_respects_its_import_ratchet(root: Path, allowed: Callable[[str], bool]) -> None:
    assert (root / "__init__.py").is_file(), root
    violations = scan(root, allowed)
    assert not violations, "\n".join(map(str, violations))


PLANTED = [
    pytest.param(
        "import json\nimport pydantic\n", stdlib_and("alkera"), "pydantic", id="third-party"
    ),
    pytest.param(
        "from alkera_cli.x import y\n", stdlib_and("alkera"), "alkera_cli.x", id="from-import"
    ),
    pytest.param(
        "def f():\n    import numpy as np\n", stdlib_and("_alkera_kernel"), "numpy", id="nested"
    ),
    pytest.param(
        "import alkera\n", stdlib_and("_alkera_kernel"), "alkera", id="kernel-imports-alkera"
    ),
    pytest.param(
        "import importlib\nimportlib.import_module('requests')\n",
        stdlib_and("alkera"),
        "requests",
        id="import-module-string",
    ),
    pytest.param("import loro\n", not_platform, "loro", id="notebook-loro"),
    pytest.param(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from alkera_core.db import x\n",
        not_platform,
        "alkera_core.db",
        id="notebook-type-checking",
    ),
    pytest.param(
        "import backend.app_factory\n", not_platform, "backend.app_factory", id="notebook-backend"
    ),
]


def test_the_platform_is_every_other_first_party_package() -> None:
    assert {"alkera_core", "alkera_cli", "backend", "loro"} <= PLATFORM
    assert not PLATFORM & NOTEBOOK_PACKAGES


@pytest.mark.parametrize(("source", "allowed", "module"), PLANTED)
def test_scan_catches_a_planted_violation(
    tmp_path: Path, source: str, allowed: Callable[[str], bool], module: str
) -> None:
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "planted.py").write_text(source, encoding="utf-8")
    assert [v.module for v in scan(tmp_path, allowed)] == [module]


ALLOWED = [
    pytest.param(
        "from __future__ import annotations\nimport os, sys\nfrom collections import abc\n",
        stdlib_and("alkera"),
        id="stdlib",
    ),
    pytest.param(
        "import alkera.ui\nfrom . import _sql\n", stdlib_and("alkera"), id="self-and-relative"
    ),
    pytest.param("import pydantic\nimport msgspec\n", not_platform, id="notebook-third-party"),
    pytest.param("import alkera_notebook_extra\n", not_platform, id="prefix-is-not-platform"),
]


@pytest.mark.parametrize(("source", "allowed"), ALLOWED)
def test_scan_admits_allowed_imports(
    tmp_path: Path, source: str, allowed: Callable[[str], bool]
) -> None:
    (tmp_path / "ok.py").write_text(source, encoding="utf-8")
    assert scan(tmp_path, allowed) == []
