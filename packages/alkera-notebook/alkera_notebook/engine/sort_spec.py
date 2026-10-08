"""The table sort syntax: ``col:asc,col2:desc``.

One reader of the text form, shared by every surface that takes it (the
platform's table page route, the agent's ``notebook.inspect``): it checks the
syntax only. Whether each column exists is the kernel's to say, since it alone
sees the frame (an unknown column is its ``inspect.unknown_column`` error).
"""

from __future__ import annotations

from typing import Any, Final

#: The most columns a table page may be sorted by.
MAX_SORT_KEYS: Final = 16
#: The longest column name a sort key may carry.
MAX_SORT_COLUMN_CHARS: Final = 256


def parse_sort_spec(raw: str | None) -> list[dict[str, Any]]:
    """``col:asc,col2:desc`` (the direction defaults to ascending) as the
    kernel's sort keys ``[{column, descending}]``, in order.

    The direction is what follows the last ``:`` of a part, so a column name
    may itself hold a ``:`` when the direction is written. Raises
    :class:`ValueError` for a part that does not read: an empty column, a
    direction other than ``asc`` or ``desc``, a column longer than
    :data:`MAX_SORT_COLUMN_CHARS`, or more than :data:`MAX_SORT_KEYS` keys."""
    if raw is None or raw.strip() == "":
        return []
    keys: list[dict[str, Any]] = []
    for part in raw.split(","):
        column, _, direction = part.rpartition(":") if ":" in part else (part, "", "asc")
        column = column.strip()
        direction = direction.strip().lower() or "asc"
        if not column or len(column) > MAX_SORT_COLUMN_CHARS or direction not in ("asc", "desc"):
            raise ValueError(f"{part!r} is not a column and a direction")
        keys.append({"column": column, "descending": direction == "desc"})
    if len(keys) > MAX_SORT_KEYS:
        raise ValueError(f"a page is sorted by at most {MAX_SORT_KEYS} columns")
    return keys


__all__ = ["MAX_SORT_COLUMN_CHARS", "MAX_SORT_KEYS", "parse_sort_spec"]
