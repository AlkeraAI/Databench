"""The kernel package's real-kernel test harness, importable from here.

The fixtures are imported into each test module that uses them rather than
through a ``conftest.py``: this directory sits on ``sys.path`` (the
``alkera`` distribution is installed from it), so a ``conftest.py`` in this folder
would shadow ``tests.conftest`` for every other suite that imports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_KERNEL_TESTS = Path(__file__).resolve().parents[2] / "alkera-kernel" / "tests" / "kernel"
if str(_KERNEL_TESTS) not in sys.path:
    sys.path.insert(0, str(_KERNEL_TESTS))

from nbkrn_harness import (  # noqa: E402
    KernelFactory,
    RunResult,
    kernel_mount,
    rich_python,
    start_kernel,
    step,
)

__all__ = ["KernelFactory", "RunResult", "kernel_mount", "rich_python", "start_kernel", "step"]
