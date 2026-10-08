"""The oldest Python ``alkera`` supports, read from its ``requires-python``.

The tests that prove the floor (the source gate, the import probe, the SQL
decode environments) read it here, so raising or lowering the floor is one edit
to ``packages/alkera-py/pyproject.toml``. Standard library only, and importable
on the floor interpreter itself.
"""

from __future__ import annotations

import re
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

_REQUIRES = re.compile(r'^requires-python\s*=\s*">=\s*(\d+)\.(\d+)"\s*$', re.MULTILINE)


def declared_floor(pyproject: Path = PYPROJECT) -> tuple[int, int]:
    """``(major, minor)`` from a plain ``requires-python = ">=X.Y"``."""
    found = _REQUIRES.search(pyproject.read_text(encoding="utf-8"))
    if found is None:
        raise ValueError(f'{pyproject} has no plain requires-python = ">=X.Y" floor')
    return int(found[1]), int(found[2])


def floor_version() -> str:
    """The floor as ``uv`` and ``python`` spell it, ``"3.8"``."""
    major, minor = declared_floor()
    return f"{major}.{minor}"
