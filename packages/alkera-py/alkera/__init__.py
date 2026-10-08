"""The public ``alkera`` API for notebooks and data tests.

Standard library only, Python 3.8 and later. The same calls work inside
the Alkera notebook kernel, under stock marimo, in Jupyter and in plain
Python; ``alkera._host`` decides where output, interrupts and service calls
go. See the package README for every call.
"""

from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Any, TypeVar

from . import _host, output, status
from ._cache import persistent_cache
from ._outputs import callout, hstack, image, md, vstack
from ._outputs import html_ as html
from .errors import ConnectionNotConfigured, FeatureMissing, ServiceUnavailable, StopCell
from .testing import Connection, RegisteredTest, RowSet, registered_tests, reset_registered, test

__test__ = False  # keep pytest from collecting the module-level `test` symbol

T = TypeVar("T")


def stop(predicate: object, output: object | None = None) -> None:
    """End the cell here when ``predicate`` is true, showing ``output``
    first. The cell counts as stopped, not failed, and cells that depend on
    it do not run."""
    if not predicate:
        return
    host = _host.current()
    stop_type = host.stop_exception()
    if getattr(stop_type, _host.CARRIES_OUTPUT, False):
        raise stop_type(output)
    if output is not None:
        host.display(output)
    raise stop_type()


def widget(obj: T) -> T:
    """Make ``obj`` (an ipywidget or anywidget) reactive: when its value
    changes, the cells that read it run again. Returns ``obj``."""
    _host.current().register_reactive(obj)
    return obj


def args() -> MappingProxyType[str, Any]:
    """The arguments the notebook was run with (``alkera-notebook run
    file -- --key value``), read-only. Empty in the editor."""
    return MappingProxyType(dict(_host.current().args()))


def call(name: str, /, **params: Any) -> Any:
    """Call the Alkera service method ``name`` with ``params``. Only inside
    the Alkera kernel; elsewhere it raises ``ServiceUnavailable``."""
    return _host.current().call(name, params)


_LAZY = {
    "ui": (".ui", None, "alkera.ui (notebook widgets)"),
    "sql": ("._sql", "sql", "alkera.sql"),
    "chart": (".chart", None, "alkera.chart"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module 'alkera' has no attribute {name!r}")
    module_name, attribute, label = spec
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}{module_name}":
            raise
        raise FeatureMissing(f"{label} is not part of this alkera installation") from None
    value = getattr(module, attribute) if attribute else module
    globals()[name] = value
    return value


__all__ = [
    "Connection",
    "ConnectionNotConfigured",
    "FeatureMissing",
    "RegisteredTest",
    "RowSet",
    "ServiceUnavailable",
    "StopCell",
    "args",
    "call",
    "callout",
    "hstack",
    "html",
    "image",
    "md",
    "output",
    "persistent_cache",
    "registered_tests",
    "reset_registered",
    "status",
    "stop",
    "test",
    "vstack",
    "widget",
]
