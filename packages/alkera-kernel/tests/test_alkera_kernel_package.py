"""The kernel runtime is loadable by file path under a private name, the way
``boot.py`` loads it from the platform mount (never from ``sys.path``)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import _alkera_kernel

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_alkera_kernel_resolves_from_its_distribution() -> None:
    assert Path(_alkera_kernel.__file__).resolve().parent == PACKAGE_ROOT / "_alkera_kernel"


def test_kernel_loads_by_path_under_a_version_qualified_name() -> None:
    init = PACKAGE_ROOT / "_alkera_kernel" / "__init__.py"
    name = f"_alkera_kernel_v{_alkera_kernel.__version__.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(init.parent)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert module.__version__ == _alkera_kernel.__version__
    finally:
        del sys.modules[name]
