"""scripts/export_openapi.py exports the composition its caller names.

The script names no composition root of its own: the open app is the default
and any other root arrives as ``--app MODULE:ATTR``, so the script can ship in
the open tree without importing a private module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

#: A composition root with one route the open app does not serve.
ROOT_MODULE = """
from fastapi import FastAPI

app = FastAPI()
not_an_app = object()


@app.get("/only-here")
def only_here() -> dict[str, str]:
    return {}
"""


def _export(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "named_root.py").write_text(textwrap.dedent(ROOT_MODULE), encoding="utf-8")
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "PYTHONPATH": os.pathsep.join([str(SCRIPTS), str(tmp_path)]),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "export_openapi.py"), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=REPO_ROOT / "apps" / "backend",
    )


def test_a_named_root_is_exported_where_out_says(tmp_path: Path) -> None:
    out = tmp_path / "doc.json"
    result = _export(tmp_path, "--app", "named_root:app", "--out", str(out))
    assert result.returncode == 0, result.stderr[-2000:]
    assert list(json.loads(out.read_text(encoding="utf-8"))["paths"]) == ["/only-here"]


@pytest.mark.parametrize(
    ("root", "error"),
    [
        pytest.param("named_root", "MODULE:ATTR", id="no-attribute"),
        pytest.param("named_root:not_an_app", "is not a FastAPI app", id="not-an-app"),
    ],
)
def test_a_root_that_is_not_an_app_is_refused(tmp_path: Path, root: str, error: str) -> None:
    out = tmp_path / "doc.json"
    result = _export(tmp_path, "--app", root, "--out", str(out))
    assert result.returncode != 0
    assert error in result.stderr
    assert not out.exists()


def test_without_a_root_the_open_app_is_exported(tmp_path: Path) -> None:
    out = tmp_path / "doc.json"
    result = _export(tmp_path, "--out", str(out))
    assert result.returncode == 0, result.stderr[-2000:]
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["info"]["title"] == "Databench API"
    assert "/health/live" in document["paths"]
    assert "/only-here" not in document["paths"]


def test_the_exporter_runs_with_nothing_beside_it(tmp_path: Path) -> None:
    """The open tree ships the exporter without the subset tooling (that reads
    the private boundary manifest), so it must import nothing from scripts/."""
    alone = tmp_path / "tree" / "scripts"
    alone.mkdir(parents=True)
    shutil.copy(SCRIPTS / "export_openapi.py", alone)
    out = tmp_path / "doc.json"
    result = subprocess.run(
        [sys.executable, "-I", str(alone / "export_openapi.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
        cwd=REPO_ROOT / "apps" / "backend",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "/health/live" in json.loads(out.read_text(encoding="utf-8"))["paths"]
