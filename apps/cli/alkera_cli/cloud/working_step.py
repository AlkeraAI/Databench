"""The step from a held folder down to the directory its live sync watches."""

from __future__ import annotations

from pathlib import Path


def working_inside(root: Path, working_dir: Path) -> str:
    """The step from the leased folder ``root`` down to the watched directory.

    One name, not a guess: the same relative step on both sides, the step
    and not the whole path because the folder can be renamed while the box
    holds it (the id cannot, so the drive side re-derives its path).
    """
    try:
        inside = working_dir.relative_to(root).as_posix()
    except ValueError:
        return ""
    return "" if inside == "." else inside.strip("/")


__all__ = ["working_inside"]
