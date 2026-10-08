"""Every module of the agent tool framework imports first in a fresh process.

A module that only imports after something else loaded first hides an import
cycle until a child process, a worker or a command happens to import it cold,
and then it fails there and nowhere else. One interpreter imports each module
with every ``alkera_cli`` module dropped from ``sys.modules`` before it, so each
import runs as the first one."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

_COLD_IMPORTS = """
import importlib, json, pkgutil, sys

ROOT = sys.argv[1]


def modules():
    package = importlib.import_module(ROOT)
    found = [ROOT, *(info.name for info in pkgutil.walk_packages(package.__path__, ROOT + "."))]
    return sorted(set(found))


def forget():
    for name in [n for n in sys.modules if n == "alkera_cli" or n.startswith("alkera_cli.")]:
        del sys.modules[name]


names = modules()
failures = {}
for name in names:
    forget()
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures[name] = f"{type(exc).__name__}: {exc}"
print("REPORT" + json.dumps({"checked": len(names), "failures": failures}))
"""


def cold_import_report(root: str, tmp_path: Path) -> dict[str, Any]:
    """How many modules under ``root`` were imported cold, and which failed."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_COLD_IMPORTS), root],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(line for line in result.stdout.splitlines() if line.startswith("REPORT"))
    report: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return report


def test_every_tool_framework_module_imports_first(tmp_path: Path) -> None:
    report = cold_import_report("alkera_cli.plugins.plugin_base", tmp_path)
    assert report["checked"] > 50
    assert report["failures"] == {}
