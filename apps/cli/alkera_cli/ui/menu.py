"""Pure menu helpers — option model, fuzzy filter, viewport math.

Presentation-only and dependency-free: the Textual composer overlay
(`ui.tui.composer.overlay`) drives the actual interactive menu and
reuses these primitives for filtering + scroll bookkeeping. No
event-loop ownership, no terminal I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MenuOption:
    """One selectable row: a label plus an optional dim detail column."""

    label: str
    detail: str = ""


def match_options(options: Sequence[MenuOption], needle: str) -> list[int]:
    """Indices of options whose label or detail contains ``needle``
    (case-insensitive). Empty needle matches everything."""
    if not needle:
        return list(range(len(options)))
    low = needle.lower()
    return [
        i for i, opt in enumerate(options) if low in opt.label.lower() or low in opt.detail.lower()
    ]


def scroll_top(cursor: int, top: int, height: int, total: int) -> int:
    """New top row keeping ``cursor`` inside a ``height``-row viewport."""
    if total <= height:
        return 0
    if cursor < top:
        return cursor
    if cursor >= top + height:
        return cursor - height + 1
    return min(top, total - height)
