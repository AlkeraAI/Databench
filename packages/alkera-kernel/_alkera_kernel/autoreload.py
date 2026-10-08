"""Reload changed workspace modules before a run.

Before each run, modules whose source file is under the workspace root and
whose modification time changed since the last check are reloaded with
``importlib.reload``, together with the workspace modules that import them.
Each is reloaded after the changed modules it imports, so a dependent picks
up the fresh definitions rather than re-binding the stale ones.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType


class Autoreloader:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self._mtimes: dict[str, float] = {}
        self.snapshot()

    def _tracked(self) -> dict[str, ModuleType]:
        out: dict[str, ModuleType] = {}
        for name, module in list(sys.modules.items()):
            path = getattr(module, "__file__", None)
            if not isinstance(path, str) or name == "__main__":
                continue
            real = os.path.realpath(path)
            if real.startswith(self.root + os.sep) and real.endswith(".py"):
                out[name] = module
        return out

    def snapshot(self, *, only_new: bool = False) -> None:
        for name, module in self._tracked().items():
            if only_new and name in self._mtimes:
                continue
            try:
                self._mtimes[name] = os.stat(module.__file__ or "").st_mtime_ns
            except OSError:
                continue

    def reload_changed(self) -> list[str]:
        tracked = self._tracked()
        changed: list[str] = []
        for name, module in tracked.items():
            try:
                mtime = os.stat(module.__file__ or "").st_mtime_ns
            except OSError:
                continue
            if name in self._mtimes and self._mtimes[name] != mtime:
                changed.append(name)
            self._mtimes[name] = mtime
        if not changed:
            return []
        order = _dependency_order(changed, tracked)
        done: list[str] = []
        for name in order:
            try:
                importlib.reload(tracked[name])
                done.append(name)
            except Exception as exc:
                print(f"autoreload: {name} failed to reload: {exc}", file=sys.stderr)
        self.snapshot()
        return done


def _dependency_order(changed: list[str], tracked: dict[str, ModuleType]) -> list[str]:
    """Changed modules plus the tracked modules that import them, each after
    the modules it imports."""
    imports: dict[str, set[str]] = {}
    for name, module in tracked.items():
        deps = {
            v.__name__
            for v in vars(module).values()
            if isinstance(v, ModuleType) and v.__name__ in tracked
        }
        deps |= {
            getattr(v, "__module__", "")
            for v in vars(module).values()
            if getattr(v, "__module__", None) in tracked and getattr(v, "__module__", None) != name
        }
        imports[name] = deps - {name}
    affected = set(changed)
    grew = True
    while grew:
        grew = False
        for name, deps in imports.items():
            if name not in affected and deps & affected:
                affected.add(name)
                grew = True
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dep in sorted(imports.get(name, ())):
            if dep in affected:
                visit(dep)
        order.append(name)

    for name in sorted(affected):
        visit(name)
    return order
