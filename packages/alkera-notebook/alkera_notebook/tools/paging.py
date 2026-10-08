"""The one paging and size budget every notebook tool result goes through.

A notebook can hold thousands of cells, cells thousands of lines, outputs
megabytes of text and frames millions of rows; a tool result is read by a
model whose context is finite. So every list a tool returns is a page and
every long text a window, and both follow the same rules, taken from the
agent's own file tools:

- A page is ``offset`` and ``limit`` over a list whose ``total`` the result
  states; the limit has a default and a hard cap the input refuses past.
- A result never weighs more than :data:`RESULT_BUDGET_BYTES` as JSON. When
  the page or window would, it is cut short, and the cut is stated: the page
  says ``cut_by_budget``. Under a host that stores results (the CLI harness
  spills every too-large tool reply, SQL's included, to its blob store), what
  was cut goes there whole: the page carries ``blob``, a handle the agent
  reads with the host's blob paging tool (``fetch_result``), one whole item
  per row (a cell, an edge, a variable) or the window's text by character.
  The tool result names the same handle at its top level, as a SQL result
  does, so the chat shows it as the same reference. Without such a host,
  ``more`` carries the exact arguments of the call that returns the rest.
- Every item in a page is bounded on its own (a cell's names, a value's repr,
  a table cell's text), so a page always holds at least one item and walking
  ``next_offset`` always makes progress.

Sizes are measured, never estimated: :func:`measure` is the JSON a transport
sends, and the longest page that fits is found by a search over measured
candidates (:func:`fit_prefix`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

#: The most a notebook tool result weighs, as JSON bytes. A harness bounds what
#: it puts inline (the CLI harness at 64 KiB); this stays under it with room for the
#: notebook guide the first result of a conversation carries.
RESULT_BUDGET_BYTES = 48 * 1024

#: What a spilled result's handle costs inline, at most, twice over (on the page
#: it covers and at the result's top level, with its name and type): reserved
#: from the budget whenever results spill, so the handle never pushes a reply
#: past it.
SPILL_RESERVE_BYTES = 768

#: The most one text field (a value's repr, a widget value, a table cell) weighs
#: inside a page, so no single item can crowd out the rest.
ITEM_TEXT_CHARS = 1_000

T = TypeVar("T")
U = TypeVar("U")
R = TypeVar("R", bound=BaseModel)


class ResultSpill(Protocol):
    """Where a host stores a reply too large to inline. Each returns the
    host's handle (the CLI harness's ``{"sha256", "size", "media_type"}``, opaque
    here), or ``None`` when it could not store it (the reply then falls back
    to ``more``)."""

    def rows(self, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
        """Store a list, one whole item per row."""
        ...

    def text(self, text: str) -> dict[str, Any] | None:
        """Store a text, read back by character."""
        ...


@dataclass
class SpillScope:
    """One tool call's spill port and the handles it made, in order."""

    port: ResultSpill
    made: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


_SCOPE: ContextVar[SpillScope | None] = ContextVar("notebook_result_spill", default=None)


@contextmanager
def spilling(port: ResultSpill | None) -> Iterator[SpillScope | None]:
    """Run one tool call with ``port`` as its spill (``call_tool`` opens it;
    nothing else should): a page or window cut by the budget is stored whole
    through it."""
    scope = SpillScope(port) if port is not None else None
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def result_budget() -> int:
    """The bytes a result may weigh in the current call: the budget, less the
    room a spilled handle takes when results spill."""
    if _SCOPE.get() is None:
        return RESULT_BUDGET_BYTES
    return RESULT_BUDGET_BYTES - SPILL_RESERVE_BYTES


def _as_row_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        dumped = item.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(item, Mapping):
        return {str(k): v for k, v in item.items()}
    return {"value": item}


def spill_items(items: Sequence[Any]) -> dict[str, Any] | None:
    """Store ``items`` whole, one per row, through the call's spill; the
    handle, or ``None`` without a spill or when the store failed."""
    scope = _SCOPE.get()
    if scope is None or not items:
        return None
    dicts = [_as_row_dict(item) for item in items]
    columns: list[str] = []
    for d in dicts:
        columns.extend(k for k in d if k not in columns)
    ref = scope.port.rows(columns, [[d.get(c) for c in columns] for d in dicts])
    if ref is not None:
        scope.made.append(("rows", ref))
    return ref


def spill_table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
    """Store a table whole through the call's spill, its columns as they are
    (a table may name two columns alike, which a row dict would merge)."""
    scope = _SCOPE.get()
    if scope is None or not rows:
        return None
    ref = scope.port.rows(columns, rows)
    if ref is not None:
        scope.made.append(("rows", ref))
    return ref


def spill_text(text: str) -> dict[str, Any] | None:
    """Store ``text`` through the call's spill (read back by character)."""
    scope = _SCOPE.get()
    if scope is None or not text:
        return None
    ref = scope.port.text(text)
    if ref is not None:
        scope.made.append(("text", ref))
    return ref


class NotebookMore(BaseModel):
    """The call that returns what a result left out, ready to send as is."""

    tool: str
    args: dict[str, Any]
    note: str = ""


class NotebookPage(BaseModel):
    """Where a page sits in its list."""

    offset: int
    limit: int
    returned: int
    total: int
    next_offset: int | None = None
    """Where the next page starts; ``None`` when this page reaches the end."""
    cut_by_budget: bool = False
    """True when the page holds fewer than ``limit`` items because the result
    would otherwise exceed the size budget."""
    blob: dict[str, Any] | None = None
    """When the page was cut: the whole page (items ``offset`` to
    ``offset + limit``), one whole item per row, stored for ``fetch_result``."""
    more: NotebookMore | None = None


class NotebookTextPage(BaseModel):
    """Where a window of text sits in the whole text."""

    unit: Literal["line", "char"]
    offset: int
    """The first line (0-based) or character of the window."""
    returned: int
    total: int
    next_offset: int | None = None
    cut_by_budget: bool = False
    blob: dict[str, Any] | None = None
    """When the window was cut: the window's whole text, stored for
    ``fetch_result`` (read by character)."""
    more: NotebookMore | None = None


def json_bytes(value: Any) -> int:
    """What ``value`` weighs as JSON, as a transport writes it."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", "replace"))


def measure(result: BaseModel) -> int:
    """What a tool result weighs as JSON."""
    return json_bytes(result.model_dump(mode="json"))


def fits(result: BaseModel, limit: int | None = None) -> bool:
    return measure(result) <= (result_budget() if limit is None else limit)


def fit_prefix(n: int, ok: Callable[[int], bool], *, floor: int = 0) -> int:
    """The largest ``k <= n`` with ``ok(k)``, never below ``floor``. ``ok`` must
    be monotone (a shorter prefix never weighs more)."""
    if n <= floor or ok(n):
        return n
    lo, hi = floor, n - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def more_call(tool: str, args: BaseModel, note: str, **changes: Any) -> NotebookMore:
    """The call that continues ``args``: the same arguments a model sent (as it
    sent them, defaults left out) with ``changes`` applied."""
    sent = args.model_dump(mode="json", exclude_defaults=True)
    sent.setdefault("path", getattr(args, "path", ""))
    return NotebookMore(tool=tool, args={**sent, **changes}, note=note)


def page_at(
    *,
    offset: int,
    limit: int,
    total: int,
    returned: int,
    tool: str,
    args: BaseModel,
    offset_arg: str = "offset",
    noun: str = "items",
    spilled: tuple[dict[str, Any], int] | None = None,
    **also: Any,
) -> NotebookPage:
    """The place of a page of ``returned`` items from ``offset`` in a list of
    ``total``, and the call for the rest (``also`` adds arguments to it).
    ``spilled`` is the handle holding the whole page and how many items it
    holds: the rest of the page is read from it, and the call for the rest
    starts after it."""
    end = offset + returned
    cut = returned < min(limit, total - offset)
    if spilled is not None:
        ref, held = spilled
        after = offset + held
        return NotebookPage(
            offset=offset,
            limit=limit,
            returned=returned,
            total=total,
            next_offset=after if after < total else None,
            cut_by_budget=cut,
            blob=ref,
            more=(
                more_call(
                    tool,
                    args,
                    f"{total - after} more {noun} after this page",
                    **{offset_arg: after},
                    **also,
                )
                if after < total
                else None
            ),
        )
    has_more = end < total
    note = f"{total - end} more {noun}"
    if cut:
        note += " (this page was cut to stay under the size budget)"
    return NotebookPage(
        offset=offset,
        limit=limit,
        returned=returned,
        total=total,
        next_offset=end if has_more else None,
        cut_by_budget=cut,
        more=more_call(tool, args, note, **{offset_arg: end}, **also) if has_more else None,
    )


def page_of(
    items: Sequence[T],
    *,
    offset: int,
    limit: int,
    render: Callable[[T], U],
    build: Callable[[list[U], NotebookPage], R],
    tool: str,
    args: BaseModel,
    offset_arg: str = "offset",
    noun: str = "items",
    budget: int | None = None,
) -> R:
    """The result ``build`` makes from the longest page of ``items`` from
    ``offset`` (at most ``limit``, each item rendered once) that keeps it under
    ``budget``, with the page's place stated. A page the budget cuts is stored
    whole through the call's spill (one whole item per row) and the page names
    it; without one, the page names the call for the rest."""
    total = len(items)
    start = min(offset, total)
    window = [render(item) for item in items[start : start + limit]]
    cap = result_budget() if budget is None else budget

    def make(k: int, spilled: tuple[dict[str, Any], int] | None = None) -> R:
        page = page_at(
            offset=start,
            limit=limit,
            total=total,
            returned=k,
            tool=tool,
            args=args,
            offset_arg=offset_arg,
            noun=noun,
            spilled=spilled,
        )
        return build(window[:k], page)

    k = fit_prefix(len(window), lambda k: fits(make(k), cap), floor=min(1, len(window)))
    if k == len(window):
        return make(k)
    ref = spill_items(window)
    if ref is None:
        return make(k)
    held = (ref, len(window))
    k = fit_prefix(len(window), lambda k: fits(make(k, held), cap), floor=min(1, len(window)))
    return make(k, held)


def clip_text(text: str, max_chars: int = ITEM_TEXT_CHARS) -> str:
    """``text`` cut to ``max_chars``, with a marker saying how much was left out."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f" ... [{len(text) - max_chars} characters omitted]"


def clip_value(value: Any, max_chars: int = ITEM_TEXT_CHARS) -> Any:
    """``value`` itself when its JSON is short, or its JSON text clipped: an item
    in a page (a widget value, a table cell) never weighs more than a line."""
    if isinstance(value, str):
        return clip_text(value, max_chars)
    if value is None or isinstance(value, bool | int | float):
        return value
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= max_chars else clip_text(text, max_chars)


def text_window(
    text: str,
    *,
    unit: Literal["line", "char"],
    offset: int,
    limit: int,
    max_bytes: int,
    tool: str,
    args: BaseModel,
    offset_arg: str,
    also: dict[str, Any] | None = None,
) -> tuple[str, NotebookTextPage]:
    """A window of ``text``: ``limit`` lines (or characters) from ``offset``,
    cut shorter when it would weigh more than ``max_bytes`` as JSON, with the
    window's place stated and the call that reads on. A window always holds at
    least one character, so reading on always progresses."""
    pieces = text.splitlines(keepends=True) if unit == "line" else []
    total = len(pieces) if unit == "line" else len(text)
    start = min(offset, total)
    if unit == "line":
        window = pieces[start : start + limit]
        k = fit_prefix(
            len(window),
            lambda k: json_bytes("".join(window[:k])) <= max_bytes,
            floor=min(1, len(window)),
        )
        body = "".join(window[:k])
        if k == 1 and json_bytes(body) > max_bytes:
            # One line longer than the window: its head, and the rest is read by
            # character offset in the cell's or output's text.
            cut = fit_prefix(len(body), lambda n: json_bytes(body[:n]) <= max_bytes, floor=1)
            body = body[:cut]
        end = start + k
    else:
        chunk = text[start : start + limit]
        k = fit_prefix(len(chunk), lambda n: json_bytes(chunk[:n]) <= max_bytes, floor=1)
        body = chunk[:k]
        end = start + len(body)
    has_more = end < total
    cut = end - start < min(limit, total - start)
    noun = "lines" if unit == "line" else "characters"
    if cut:
        whole = (
            "".join(pieces[start : start + limit])
            if unit == "line"
            else text[start : start + limit]
        )
        ref = spill_text(whole)
        if ref is not None:
            after = start + (len(pieces[start : start + limit]) if unit == "line" else len(whole))
            return body, NotebookTextPage(
                unit=unit,
                offset=start,
                returned=end - start,
                total=total,
                next_offset=after if after < total else None,
                cut_by_budget=True,
                blob=ref,
                more=(
                    more_call(
                        tool,
                        args,
                        f"{total - after} more {noun} after this window",
                        **{offset_arg: after, **(also or {})},
                    )
                    if after < total
                    else None
                ),
            )
    page = NotebookTextPage(
        unit=unit,
        offset=start,
        returned=end - start,
        total=total,
        next_offset=end if has_more else None,
        cut_by_budget=cut,
        more=(
            more_call(tool, args, f"{total - end} more {noun}", **{offset_arg: end, **(also or {})})
            if has_more
            else None
        ),
    )
    return body, page


__all__ = [
    "ITEM_TEXT_CHARS",
    "RESULT_BUDGET_BYTES",
    "SPILL_RESERVE_BYTES",
    "NotebookMore",
    "NotebookPage",
    "NotebookTextPage",
    "ResultSpill",
    "SpillScope",
    "clip_text",
    "clip_value",
    "fit_prefix",
    "fits",
    "json_bytes",
    "measure",
    "more_call",
    "page_at",
    "page_of",
    "result_budget",
    "spill_items",
    "spill_text",
    "spilling",
    "text_window",
]
