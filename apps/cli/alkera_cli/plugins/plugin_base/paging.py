"""The one stop rule for a vendor's cursor listing.

The sync reads a listing as the whole truth of a connection's scope and deletes
whatever the listing fails to name, so a walk that ends anywhere short of a
proven end silently deletes knowledge. Each vendor adapter maps its wire shape
into :class:`Page` and the walk here decides, once, what counts as an end: a
proven well-formed absence of a cursor, and nothing else. Every other shape
raises through the vendor's own ``damaged`` error, so the pass stops loudly and
retries instead of reconciling a shortened listing as authoritative.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

#: Builds the vendor's own error around a description of what arrived, so a
#: refusal names the vendor and the walk never has to know it.
Damaged = Callable[[str], Exception]


@dataclass(frozen=True, slots=True)
class Page:
    """One listing page, already proven readable. ``next_cursor`` is ``None``
    only for a proven well-formed end, never for a shape the reader could not
    tell from one."""

    rows: tuple[dict[str, Any], ...]
    next_cursor: str | None

    @classmethod
    def read(
        cls,
        body: dict[str, Any],
        *,
        rows_key: str,
        cursor_key: str,
        more_key: str | None = None,
        damaged: Damaged,
    ) -> Page:
        """Read one vendor page, or raise ``damaged`` on any shape that could
        hide a shortened listing.

        The rows must be a list of objects. The cursor must be a non-empty
        string or a well-formed absence; ``cursor_key`` may be a dotted path,
        and an absent ancestor or a non-mapping step is damage, since an end
        cannot be told from it. A vendor that publishes a more-flag names it in
        ``more_key``, and the flag must be a bool that agrees with the cursor:
        a promise of more with no cursor, and a cursor beside a denial, both
        raise."""
        rows = body.get(rows_key)
        if not isinstance(rows, list):
            raise damaged(f"no {rows_key} list where the listing's rows belong")
        for row in rows:
            if not isinstance(row, dict):
                raise damaged(f"a {rows_key} row that is not an object")
        cursor = _cursor(body, cursor_key, damaged)
        if more_key is not None:
            more = body.get(more_key)
            if not isinstance(more, bool):
                raise damaged(f"no usable {more_key} flag saying whether the listing continues")
            if more is not (cursor is not None):
                raise damaged(f"a {more_key} flag that disagrees with its {cursor_key} cursor")
        return cls(rows=tuple(rows), next_cursor=cursor)


def _cursor(body: dict[str, Any], cursor_key: str, damaged: Damaged) -> str | None:
    holder: Any = body
    *ancestors, leaf = cursor_key.split(".")
    for step in ancestors:
        holder = holder.get(step)
        if not isinstance(holder, dict):
            raise damaged(f"no {step} object where the {cursor_key} cursor lives")
    value = holder.get(leaf)
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise damaged(f"an unusable {cursor_key} cursor")


def walk(fetch: Callable[[str | None], Page], damaged: Damaged) -> Iterator[dict[str, Any]]:
    """Every row of a listing, across pages, ending only on a proven end.

    ``fetch`` takes the cursor to request, ``None`` for the first page. A cursor
    the walk has already followed raises, and that one matters more than any
    degraded answer: following it again never returns, so the pass records
    nothing and retries nothing, and from outside it reads as merely slow."""
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = fetch(cursor)
        yield from page.rows
        cursor = page.next_cursor
        if cursor is None:
            return
        if cursor in seen:
            raise damaged("a repeated pagination cursor, so this listing never ends")
        seen.add(cursor)
