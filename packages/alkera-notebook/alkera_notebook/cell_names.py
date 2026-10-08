"""How a person sees a cell named: the one function every person-facing line uses.

A cell has an internal id (``a7yg9x7evz``) and a name. The id is how agents and
the engine address a cell; it means nothing to a person reading a prompt, a run
summary or an error. The name is ``_`` for a cell nobody named (the marimo
convention), and an underscore means nothing to a person either.

So a cell is shown by its name when it has one, otherwise by its position in
the notebook ("Cell 4"), counted from 1 in notebook order. Never by its id.
Agent-facing payloads keep the id, because agents need it to address the cell.

:func:`resolve_cell_ref` is the inverse: it reads a reference the way a person
or an agent writes one (an id, a name, ``Cell 4``, ``4``, ``first``, ``last``)
and returns the cell's id, so every way a cell is shown is also a way to
address it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: The name a cell carries when nobody named it.
ANONYMOUS_CELL_NAME = "_"

#: How a cell reads when neither a name nor a position is known.
UNPLACED_CELL = "A cell"


def cell_display_name(name: str | None, index: int | None) -> str:
    """The cell as a person sees it: its name if set, else ``Cell N`` from its
    0-based ``index`` in notebook order, else ``A cell``."""
    if name and name != ANONYMOUS_CELL_NAME:
        return name
    if index is not None and index >= 0:
        return f"Cell {index + 1}"
    return UNPLACED_CELL


#: ``Cell 4`` or ``4``: a position counted from 1, as :func:`cell_display_name` shows it.
_POSITION = re.compile(r"^(?:cell\s*)?(\d{1,6})$", re.IGNORECASE)
#: Words that name a position.
_ENDS = {"first": 0, "last": -1}


class CellRefError(LookupError):
    """A reference that names no cell (``code`` ``cell_not_found``) or more
    than one (``ambiguous_cell``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_cell_ref(ref: str, cells: Sequence[tuple[str, str]]) -> str:
    """The id of the cell ``ref`` names among ``cells`` (``(id, name)`` in
    notebook order).

    In order: an id; a name (``_`` names no one cell); ``first`` or ``last``;
    a position counted from 1, written ``Cell 4`` or ``4``. A cell named
    ``last`` is that cell, not the last one: a name wins over a word. Raises
    :class:`CellRefError` when nothing matches or a name is shared."""
    text = ref.strip()
    for cid, _name in cells:
        if cid == text:
            return cid
    if text != ANONYMOUS_CELL_NAME:
        named = [cid for cid, name in cells if name == text]
        if len(named) > 1:
            raise CellRefError("ambiguous_cell", f"{len(named)} cells are named {text!r}.")
        if named:
            return named[0]
    if not cells:
        raise CellRefError("cell_not_found", f"No cell {ref!r}: the notebook has no cells.")
    end = _ENDS.get(text.lower())
    if end is not None:
        return cells[end][0]
    position = _POSITION.match(text)
    if position is not None:
        number = int(position.group(1))
        if 1 <= number <= len(cells):
            return cells[number - 1][0]
        raise CellRefError(
            "cell_not_found", f"No cell {ref!r}: the notebook has {len(cells)} cells."
        )
    raise CellRefError(
        "cell_not_found",
        f"No cell {ref!r}. Name a cell by its id, its name, its position (Cell 3), first or last.",
    )


__all__ = [
    "ANONYMOUS_CELL_NAME",
    "UNPLACED_CELL",
    "CellRefError",
    "cell_display_name",
    "resolve_cell_ref",
]
