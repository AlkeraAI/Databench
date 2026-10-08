"""Post-import hooks: run a callback right after a named module is first
imported by the person's code, without the kernel importing it itself.

Used to install the ``comm`` provider when ``comm`` is imported, and to make
``matplotlib.pyplot.show`` display into the current cell.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import threading
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import Any

Callback = Callable[[ModuleType], None]


class _Loader(importlib.abc.Loader):
    def __init__(self, inner: importlib.abc.Loader, callbacks: list[Callback]) -> None:
        self._inner = inner
        self._callbacks = callbacks

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self._inner.exec_module(module)
        for callback in self._callbacks:
            try:
                callback(module)
            except Exception as exc:  # a hook must never break the import
                print(
                    f"alkera: post-import hook for {module.__name__} failed: {exc}",
                    file=sys.__stderr__,
                )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class PostImportFinder(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self._hooks: dict[str, list[Callback]] = {}
        self._local = threading.local()

    def add(self, name: str, callback: Callback) -> None:
        module = sys.modules.get(name)
        if module is not None:
            callback(module)
            return
        self._hooks.setdefault(name, []).append(callback)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        callbacks = self._hooks.get(fullname)
        if not callbacks or getattr(self._local, "busy", False):
            return None
        self._local.busy = True
        try:
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    break
            else:
                return None
        finally:
            self._local.busy = False
        if spec.loader is None:
            return None
        del self._hooks[fullname]
        spec.loader = _Loader(spec.loader, callbacks)
        return spec


FINDER = PostImportFinder()


def install() -> None:
    if FINDER not in sys.meta_path:
        sys.meta_path.insert(0, FINDER)


def after_import(name: str, callback: Callback) -> None:
    FINDER.add(name, callback)


#: The person's optional libraries the runtime may load on its own (the
#: ``libs`` of ``hello``). Anything else is never imported by the kernel.
OPTIONAL_LIBRARIES = frozenset(
    {
        "pyarrow",
        "polars",
        "pandas",
        "numpy",
        "duckdb",
        "matplotlib",
        "ipywidgets",
        "anywidget",
        "plotly",
        "altair",
        "IPython",
    }
)


def optional_library(name: str) -> ModuleType:
    """Import one of the person's optional libraries by name (``ImportError``
    when the environment does not have it)."""
    if name not in OPTIONAL_LIBRARIES:
        raise ValueError(f"{name} is not an optional library of the kernel")
    import importlib

    return importlib.import_module(name)
