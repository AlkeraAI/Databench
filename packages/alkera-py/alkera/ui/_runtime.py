"""How ``alkera.sql`` and ``alkera.ui`` reach the host they run under, and
the person's optional data libraries.

The host is ``alkera._host.current()`` when that module is present, else the
object the Alkera kernel publishes as ``sys.modules["_alkera_runtime"].host``
(host protocol 1); with neither, code runs as a plain script.

``alkera`` depends on nothing outside the standard library. The libraries
below are the person's own, used when installed (they are what the kernel
reports in ``hello``'s ``libs``); asking for one that is missing raises
``ModuleNotFoundError`` naming it, which the kernel turns into an offer to
install it.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

OPTIONAL_LIBRARIES = ("polars", "pandas", "pyarrow", "duckdb")
RUNTIME_MODULE = "_alkera_runtime"
PROTOCOL_VERSION = 1


def host() -> Any | None:
    """The current host, or ``None`` outside any host."""
    try:
        selector: Any = importlib.import_module("alkera._host")
    except ImportError:
        selector = None
    if selector is not None:
        current = getattr(selector, "current", None)
        if callable(current):
            found = current()
            if found is not None and getattr(found, "name", "") != "script":
                return found
    runtime = sys.modules.get(RUNTIME_MODULE)
    published = getattr(runtime, "host", None) if runtime is not None else None
    if published is not None and getattr(published, "protocol_version", None) == PROTOCOL_VERSION:
        return published
    return None


def optional(name: str) -> Any:
    """One of the person's optional libraries, or ``ModuleNotFoundError``."""
    if name not in OPTIONAL_LIBRARIES:
        raise ValueError(f"{name} is not an optional library alkera uses")
    return importlib.import_module(name)


def available(name: str) -> bool:
    try:
        optional(name)
    except ImportError:
        return False
    return True
