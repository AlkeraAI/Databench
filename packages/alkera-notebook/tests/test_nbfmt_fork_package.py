"""The generated private fork package and its generator."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.xdist_group("nbfmt_fork_package")

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "gen_alkera_marimo.py"
PACKAGE = REPO / "packages" / "alkera-notebook" / "alkera_notebook" / "_marimo"
MODULES = frozenset({"marimo", "marimo._ast", "marimo._ast.parse", "marimo._ipc.launch_kernel"})


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_alkera_marimo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_alkera_marimo"] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_package_matches_vendor() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, cwd=REPO
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "from marimo._ast.parse import x\n",
            "from alkera_notebook._marimo._ast.parse import x\n",
            id="from-import",
        ),
        pytest.param(
            "from marimo import _loggers\n",
            "from alkera_notebook._marimo import _loggers\n",
            id="from-package",
        ),
        pytest.param(
            "from marimo._ast import (\n    parse,\n)\n",
            "from alkera_notebook._marimo._ast import (\n    parse,\n)\n",
            id="multi-line",
        ),
        pytest.param("import marimo\n", "import alkera_notebook._marimo as marimo\n", id="bare"),
        pytest.param(
            "import marimo as mo, os\n", "import alkera_notebook._marimo as mo, os\n", id="alias"
        ),
        pytest.param(
            "def f():\n    import marimo._ast.parse as p\n",
            "def f():\n    import alkera_notebook._marimo._ast.parse as p\n",
            id="nested",
        ),
        pytest.param(
            'args = ["-m", "marimo._ipc.launch_kernel"]\n',
            'args = ["-m", "alkera_notebook._marimo._ipc.launch_kernel"]\n',
            id="module-string",
        ),
        pytest.param(
            'calls = ["mo.sql", "marimo.sql"]\ntext = "import marimo"\n',
            'calls = ["mo.sql", "marimo.sql"]\ntext = "import marimo"\n',
            id="other-strings-untouched",
        ),
        pytest.param("import marimo_extras\n", "import marimo_extras\n", id="prefix-only"),
        pytest.param(
            "x = 'é'; import marimo\n",
            "x = 'é'; import alkera_notebook._marimo as marimo\n",
            id="unicode-offset",
        ),
    ],
)
def test_rewrite_source(gen: ModuleType, source: str, expected: str) -> None:
    assert gen.rewrite_source(source, MODULES, "t.py") == expected


def test_an_import_that_binds_marimo_implicitly_is_refused(gen: ModuleType) -> None:
    with pytest.raises(gen.GenerationError, match="implicitly"):
        gen.rewrite_source("import marimo._ast\n", MODULES, "t.py")


@pytest.mark.parametrize(
    ("text", "notebook"),
    [
        pytest.param("import marimo\napp = marimo.App()\n", True, id="notebook"),
        pytest.param("import marimo\napp, other = marimo.App(), 1\n", False, id="tuple-target"),
        pytest.param("def f():\n    app = marimo.App()\n", False, id="nested"),
        pytest.param("x = (", False, id="syntax-error"),
    ],
)
def test_is_notebook(gen: ModuleType, text: str, notebook: bool) -> None:
    assert gen.is_notebook(text) is notebook


def test_stamp_goes_after_the_leading_comments(gen: ModuleType) -> None:
    assert (
        gen.stamp("# Copyright\n# more\nimport x\n", "# NOTE")
        == "# Copyright\n# more\n# NOTE\nimport x\n"
    )


def test_generated_files_keep_their_headers_and_carry_the_notice() -> None:
    parse = (PACKAGE / "_ast" / "parse.py").read_text(encoding="utf-8").split("\n")
    assert parse[0] == "# Copyright 2026 Marimo. All rights reserved."
    header = [line for line in parse[:4] if line.startswith("#")]
    assert any(line.startswith("# Modified by Alkera: import paths rewritten") for line in header)
    assert "Apache License" in (PACKAGE / "LICENSE").read_text(encoding="utf-8")
    assert (PACKAGE / "third_party_licenses.txt").is_file()
    assert not (PACKAGE / "_static").exists()
    assert not (PACKAGE / "_smoke_tests").exists()


def test_the_fork_reports_the_vendored_version() -> None:
    from alkera_notebook._marimo import __version__

    assert __version__ == "0.25.1"
