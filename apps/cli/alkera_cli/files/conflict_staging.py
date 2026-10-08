"""The names a holder's displaced bytes wait under before the drive files them.

When the drive sends newer bytes for a file the box changed too, the box's
own bytes wait beside the file as ``.<name>.<token>.alkera-conflict`` while
they go to the drive as a conflicted copy, under a name only the drive may
choose. Until it answers they are nobody's file: never listed, never pushed,
never exported (the live sync registers the suffix as local state).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

__all__ = ["CONFLICT_SUFFIX", "conflict_staging_name", "staged_original", "staging_files"]

CONFLICT_SUFFIX: Final = ".alkera-conflict"


def conflict_staging_name(name: str, token: str) -> str:
    """The name ``name``'s displaced bytes wait under, ``token`` identifying the
    attempt. The token is also what makes a resubmission the same submission."""
    return f".{name}.{token}{CONFLICT_SUFFIX}"


def staged_original(staged: str) -> tuple[str, str] | None:
    """The file name and token a staging name encodes, or ``None``."""
    if not staged.startswith(".") or not staged.endswith(CONFLICT_SUFFIX):
        return None
    inner = staged[1 : -len(CONFLICT_SUFFIX)]
    name, dot, token = inner.rpartition(".")
    if not dot or not name or not token or name in (".", ".."):
        return None
    return name, token


def staging_files(root: Path) -> list[Path]:
    """Every conflict staging file under ``root``, links never followed."""
    found: list[Path] = []
    for directory, folders, names in os.walk(root, followlinks=False):
        folders[:] = [name for name in folders if name != ".runtime"]
        for name in names:
            if name.endswith(CONFLICT_SUFFIX) and staged_original(name) is not None:
                path = Path(directory) / name
                if not path.is_symlink() and path.is_file():
                    found.append(path)
    return sorted(found)
