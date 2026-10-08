"""``alkera_core`` never depends on the notebook engine.

The worker and the model gateway load the ORM through ``alkera_core.models``;
an import of ``alkera_notebook`` anywhere under ``alkera_core`` would pull the
engine (and the marimo fork's dependencies) into every image. The backend and
the CLI, which serve notebooks, import the engine themselves.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import alkera_core

PACKAGE_ROOT = Path(alkera_core.__file__).resolve().parent
PYPROJECT = PACKAGE_ROOT.parent / "pyproject.toml"
ENGINE = "alkera_notebook"


def _engine_imports(source: str) -> list[int]:
    """The line of every import of the engine in ``source``, at any depth
    (a function-local or ``TYPE_CHECKING`` import counts too)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name == ENGINE or name.startswith(f"{ENGINE}.") for name in names):
            lines.append(node.lineno)
    return lines


def test_the_scan_sees_every_form_of_an_engine_import() -> None:
    source = (
        "import alkera_notebook\n"
        "from alkera_notebook.engine.models import NotebookView\n"
        "def f():\n"
        "    import alkera_notebook.format as fmt\n"
        "import alkera_notebook_other\n"
        "from alkera_core.notebooks import schemas\n"
        "x = 'alkera_notebook'\n"
    )
    assert _engine_imports(source) == [1, 2, 4]


def test_nothing_under_alkera_core_imports_the_engine() -> None:
    found = [
        f"{path.relative_to(PACKAGE_ROOT.parent)}:{line}"
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        for line in _engine_imports(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_alkera_core_does_not_declare_the_engine() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    assert not [dep for dep in declared if dep.replace("_", "-").startswith("alkera-notebook")]


def test_loading_the_orm_loads_no_engine_module() -> None:
    """In a fresh interpreter (this one may have the engine loaded by another
    test), loading every model, the notebook wire shapes and the notebook
    channel leaves no ``alkera_notebook`` module behind."""
    probe = (
        "import sys\n"
        "import alkera_core.models\n"
        "import alkera_core.notebooks.schemas\n"
        "import alkera_core.notebooks.channel\n"
        "import alkera_core.notebooks.edits\n"
        "assert 'alkera_core.notebooks.models' in sys.modules\n"
        f"print(sorted(m for m in sys.modules if m.split('.')[0] == {ENGINE!r}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
