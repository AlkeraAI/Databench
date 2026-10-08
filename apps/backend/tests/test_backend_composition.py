"""Which extensions a backend script installs (``backend.composition``).

Each case runs in a fresh interpreter: installing is global to a process, and
this suite's own process already has a composition installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spread]

BACKEND = Path(__file__).resolve().parents[1]

PROBE = """
import json, sys
from backend.composition import CompositionError, install_composition
try:
    used = install_composition()
except CompositionError as error:
    print(json.dumps({"error": str(error)}))
    raise SystemExit(0)
from alkera_core.extensions import installed_extensions
print(json.dumps({
    "used": used,
    "installed": sorted(installed_extensions()),
    "probe_calls": getattr(sys.modules.get("probe_install"), "CALLS", None),
}))
"""


def _run(tmp_path: Path, install: str | None) -> dict[str, object]:
    (tmp_path / "probe_install.py").write_text(
        textwrap.dedent(
            """
            CALLS = 0

            def install():
                global CALLS
                CALLS += 1
            """
        ),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "BACKEND_INSTALL"}
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(BACKEND)])
    if install is not None:
        env["BACKEND_INSTALL"] = install
    done = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, cwd=BACKEND
    )
    assert done.returncode == 0, done.stderr
    result: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_with_nothing_set_a_script_installs_the_open_platform_only(tmp_path: Path) -> None:
    result = _run(tmp_path, None)
    assert result["used"] == "backend.open_product:install"
    assert result["installed"] == ["alkera.team-connections"]
    assert result["probe_calls"] is None


def test_a_deployment_names_its_own_installer(tmp_path: Path) -> None:
    result = _run(tmp_path, "probe_install:install")
    assert result["used"] == "probe_install:install"
    assert result["probe_calls"] == 1
    # The named installer is the whole composition: the open one is not added
    # behind its back.
    assert result["installed"] == []


@pytest.mark.parametrize(
    ("install", "says"),
    [
        pytest.param("probe_install", "is not `module:function`", id="no-function"),
        pytest.param("no_such_module:install", "cannot be loaded", id="missing-module"),
        pytest.param("probe_install:missing", "cannot be loaded", id="missing-function"),
    ],
)
def test_an_installer_that_cannot_be_called_stops_the_script(
    tmp_path: Path, install: str, says: str
) -> None:
    error = _run(tmp_path, install)["error"]
    assert isinstance(error, str)
    assert says in error
    assert install in error


@pytest.mark.parametrize("script", ["seed", "bootstrap_admin"])
def test_the_open_scripts_load_no_distributions_composition(script: str, tmp_path: Path) -> None:
    """Importing a seed script loads the open backend only; the distribution's
    composition arrives through ``BACKEND_INSTALL``, never an import."""
    env = {k: v for k, v in os.environ.items() if k != "BACKEND_INSTALL"}
    env["PYTHONPATH"] = str(BACKEND)
    # A composition root is the backend package's `product` or `main` module,
    # whatever the distribution calls its backend package.
    code = (
        f"import sys, json, scripts.{script}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('backend') "
        "and m.count('.') == 1 and m.rsplit('.', 1)[1] in ('product', 'main'))))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=BACKEND
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
