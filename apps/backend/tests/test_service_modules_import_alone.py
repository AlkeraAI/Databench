"""Every backend service module imports cleanly as the first import of a fresh interpreter.

An import cycle between service packages often stays hidden: the app's startup
order happens to import the far side of the cycle first, so the partially
initialized module is never observed. A worker, a script or a test that imports
the other side first then fails with "cannot import name ... from partially
initialized module". This test imports each module alone, in its own child
interpreter, so the order the app happens to use can't mask a cycle.

The modules are discovered by walking ``backend/services``, so a new package or
module is covered without editing this file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import backend.services

_SERVICES_ROOT = Path(backend.services.__file__).resolve().parent
_PER_IMPORT_TIMEOUT_S = 120


def _service_modules() -> list[str]:
    """Each package under ``backend.services`` and each module directly inside one."""
    modules: list[str] = []
    for package_dir in sorted(_SERVICES_ROOT.iterdir()):
        if not (package_dir / "__init__.py").is_file():
            continue
        package = f"backend.services.{package_dir.name}"
        modules.append(package)
        for child in sorted(package_dir.iterdir()):
            if child.suffix == ".py" and child.name != "__init__.py":
                modules.append(f"{package}.{child.stem}")
            elif (child / "__init__.py").is_file():
                modules.append(f"{package}.{child.name}")
    return modules


def _import_alone(module: str) -> str | None:
    """Import ``module`` first in a fresh interpreter; the error text on failure, else None."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=_PER_IMPORT_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {_PER_IMPORT_TIMEOUT_S}s"
    if result.returncode == 0:
        return None
    lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
    return lines[-1] if lines else f"exit code {result.returncode} with no stderr"


def test_discovery_covers_known_packages_and_modules() -> None:
    modules = _service_modules()
    # A broken walk that found nothing would make the import test vacuously green.
    assert "backend.services.chats" in modules
    assert "backend.services.chats.creation" in modules
    assert "backend.services.workspaces.workspace_service" in modules
    assert "backend.services.compute" in modules
    assert len(modules) > 50


def test_every_service_module_imports_alone() -> None:
    modules = _service_modules()
    workers = min(len(modules), max(2, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        errors = dict(zip(modules, pool.map(_import_alone, modules), strict=True))
    failures = {module: error for module, error in errors.items() if error is not None}
    assert not failures, "service modules that fail to import alone:\n" + "\n".join(
        f"  {module}: {error}" for module, error in sorted(failures.items())
    )
