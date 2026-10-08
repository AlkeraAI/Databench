"""The worker boots without a web framework installed.

The worker image installs ``alkera-worker`` + ``alkera-core`` only; starlette
and FastAPI arrive with the backend. Queue discovery imports every workflow and
activity module, the connection activities import the realtime event hub, and
the hub records its gauges through ``alkera_core.observability.metrics`` — so a
module-level starlette import anywhere on that path is a worker that cannot
start. Each case runs in a fresh interpreter with the two packages blocked
(``sys.modules[name] = None`` makes any import of them raise the way a missing
package does), which this process — whose fixtures import the backend — cannot
reproduce in-process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_FRAMEWORKS = ("starlette", "fastapi")

_BLOCK_WEB_FRAMEWORKS = "import sys\n" + "".join(
    f"sys.modules[{name!r}] = None\n" for name in WEB_FRAMEWORKS
)


def _import_with_web_frameworks_blocked(*modules: str) -> subprocess.CompletedProcess[str]:
    script = _BLOCK_WEB_FRAMEWORKS + "".join(f"import {module}\n" for module in modules)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize(
    "modules",
    [
        pytest.param(("worker.temporal.queues", "worker.cli"), id="container-boot-path"),
        pytest.param(("alkera_core.events",), id="event-hub-and-listener"),
        pytest.param(("alkera_core.observability.metrics",), id="metrics-gauges"),
    ],
)
def test_imports_without_a_web_framework(modules: tuple[str, ...]) -> None:
    result = _import_with_web_frameworks_blocked(*modules)
    assert result.returncode == 0, result.stderr


def test_a_module_that_needs_a_web_framework_fails_under_the_block() -> None:
    """The control: the ASGI glue genuinely needs starlette, so it must fail
    under the same block — otherwise a green above proves only that the block
    never applied."""
    result = _import_with_web_frameworks_blocked("alkera_core.observability.asgi")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ModuleNotFoundError" in result.stderr
    assert any(name in result.stderr for name in WEB_FRAMEWORKS)
