"""What importing and using the format library pulls in at run time.

The source ratchet (test_notebook_import_ratchet.py) skips the generated
fork; this one runs the format library in a fresh interpreter and lists every
module that ends up loaded, fork included.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.xdist_group("nbfmt_ratchet")

#: The platform (the format library's own runtime dependencies load nothing of
#: it) and loro. A first-party package the tree adds depends on ``alkera_core``,
#: so loading it loads that too.
FORBIDDEN = ("alkera_core", "alkera_cli", "backend", "worker", "model_gateway", "loro")

_PROBE = """
import json, sys
from alkera_notebook import format as f
text = 'import marimo\\napp = marimo.App()\\n\\n@app.cell\\ndef _():\\n'
ir = f.read(text + '    x = alkera.sql("SELECT 1")\\n    return\\n')
f.write(ir)
f.analyze(ir)
f.compile_step("_a = 1\\n_a")
print(json.dumps(sorted(sys.modules)))
"""


@pytest.fixture(scope="module")
def loaded() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


@pytest.mark.parametrize("package", FORBIDDEN)
def test_the_format_library_never_loads_the_platform(loaded: list[str], package: str) -> None:
    assert [m for m in loaded if m == package or m.startswith(package + ".")] == []


def test_the_format_library_uses_the_private_fork_not_stock_marimo(loaded: list[str]) -> None:
    assert "alkera_notebook._marimo" in loaded
    assert [m for m in loaded if m == "marimo" or m.startswith("marimo.")] == []


@pytest.mark.parametrize("package", ["starlette", "uvicorn", "websockets", "jedi", "zmq"])
def test_the_forks_server_dependencies_are_not_loaded(loaded: list[str], package: str) -> None:
    # alkera-notebook does not depend on marimo's server stack; importing the
    # fork must not need it.
    assert [m for m in loaded if m == package or m.startswith(package + ".")] == []
