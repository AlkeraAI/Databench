"""``alkera.output``: the current cell's output, written from code.

Inside the Alkera kernel these reach the cell that is running (or, from a
thread a cell started, that cell). Under stock marimo they call
``marimo.output``; in Jupyter, ``IPython.display``; in plain Python they
print.
"""

from __future__ import annotations

from typing import Any

from . import _host

__all__ = ["append", "clear", "replace"]


def append(obj: Any) -> None:
    """Add ``obj`` below the cell's output."""
    _host.current().display(obj)


def replace(obj: Any) -> None:
    """Make ``obj`` the cell's whole output."""
    _host.current().replace(obj)


def clear() -> None:
    """Remove the cell's output."""
    _host.current().clear()
