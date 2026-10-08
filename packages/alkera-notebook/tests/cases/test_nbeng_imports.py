"""Every package and module imports first, in a fresh interpreter.

A circular import only shows when the cycle is entered from the wrong end: the
outputs package imported before the engine used to fail with "partially
initialized module", while every test that imported the engine first passed.
Each case here starts a new Python so nothing is already in ``sys.modules``.
The list is discovered from the source tree, so a new module is covered
without editing this file. The vendored marimo tree is covered by its
top-level package only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import alkera_notebook
import pytest

ROOT = Path(alkera_notebook.__file__).resolve().parent


def _modules() -> list[str]:
    names: list[str] = []
    for file in sorted(ROOT.rglob("*.py")):
        rel = file.relative_to(ROOT.parent)
        parts = list(rel.with_suffix("").parts)
        if "_marimo" in parts[:-1] or parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


MODULES = _modules()


def test_discovery_sees_the_packages_that_once_formed_a_cycle() -> None:
    assert {
        "alkera_notebook.outputs",
        "alkera_notebook.plan",
        "alkera_notebook.events.models",
        "alkera_notebook.engine",
    } <= set(MODULES)


@pytest.mark.parametrize("module", [pytest.param(m, id=m) for m in MODULES])
def test_module_imports_in_a_fresh_interpreter(module: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


_RESOLVE = """
import alkera_notebook.engine as e
from alkera_notebook.engine.engine import NotebookEngine
from alkera_notebook.events.models import AnyEvent

assert e.NotebookEngine is NotebookEngine and e.AnyEvent is AnyEvent
assert {"NotebookClient", "NotebookSession", "RunHandle", "NotebookEvent"} <= set(e.__all__)
try:
    e.Missing
except AttributeError:
    pass
else:
    raise SystemExit("an unknown name resolved")
"""


def test_engine_classes_still_resolve_from_the_package() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _RESOLVE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
