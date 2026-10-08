"""Single accessor for the running CLI/daemon version.

``__version__`` (compiled into the binary from ``alkera_cli/__init__.py``) is the
**authoritative** runtime source and is kept byte-for-byte in sync with the
repo-root ``VERSION.txt`` by ``apps/cli/tests/cli/test_version.py``.

``VERSION.txt`` is ALSO shipped *inside* the binary as a data file
(the binary build's ``--include-data-files=…VERSION.txt``) so
that external tooling reading the extracted runtime dir can read the shipped
version as plain text without executing or parsing the binary.
``bundled_version()`` exposes that file; ``get_version()`` is what app code
should call.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alkera_cli import __version__

_VERSION_FILENAME = "VERSION.txt"


def get_version() -> str:
    """The authoritative running version (``alkera_cli.__version__``)."""
    return __version__


def bundled_version_path() -> Path | None:
    """Locate the shipped ``VERSION.txt`` — inside the Nuitka bundle when frozen,
    else the repo-root file in a source checkout. ``None`` if it can't be found.

    ``__compiled__`` on ``__main__`` is the "running inside a Nuitka build?" sentinel. When frozen,
    this module lives at ``<extract>/alkera_cli/host/version.py`` so ``parents[2]`` is
    the bundle root where ``VERSION.txt`` was placed.
    """
    if getattr(sys.modules.get("__main__"), "__compiled__", None):
        try:
            candidate = Path(__file__).resolve().parents[2] / _VERSION_FILENAME
        except (OSError, IndexError):
            return None
        return candidate if candidate.is_file() else None

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "Makefile").is_file():
            candidate = parent / _VERSION_FILENAME
            return candidate if candidate.is_file() else None
    return None


def bundled_version() -> str | None:
    """The version string from the shipped ``VERSION.txt``, or ``None`` if the
    file isn't present. Prefer :func:`get_version` for app logic — this exists to
    prove the shipped text matches and for tooling that reads the file directly.
    """
    path = bundled_version_path()
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


__all__ = ["bundled_version", "bundled_version_path", "get_version"]
