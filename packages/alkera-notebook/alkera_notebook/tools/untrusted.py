"""Turning notebook text into data a model reads as data.

Three rules, applied by every tool:

- Text a notebook produced is an :class:`~alkera_notebook.tools.models.Untrusted`
  envelope naming who caused it.
- An exception class name is reduced to a dotted identifier (a cell can raise a
  class named anything, and the name travels in places, like the per-turn
  digest, that carry structure only).
- A person's or agent's display name is reduced to printable text of bounded
  length before it appears as ``who`` or ``by``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from alkera_notebook.tools.models import Untrusted

#: The author named when nobody can be: text whose origin the engine does not know.
UNKNOWN_AUTHOR = "notebook"

_ENAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,4}$")
_MAX_NAME = 64


def wrap(content: Any, author: str | None) -> Untrusted:
    """``content`` as untrusted data authored by ``author``."""
    return Untrusted(author=display_name(author) or UNKNOWN_AUTHOR, content=content)


def wrap_text(text: str | None, author: str | None) -> Untrusted | None:
    """``text`` wrapped, or ``None`` when there is none."""
    if text is None or text == "":
        return None
    return wrap(text, author)


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    """``text`` cut to ``max_chars`` with a marker, and whether it was cut.

    The cut keeps the head and the tail: a traceback's cause is at its end and a
    log's context at its start, and either half alone misleads."""
    if len(text) <= max_chars:
        return text, False
    marker = f"\n... [{len(text) - max_chars} characters omitted] ...\n"
    room = max(max_chars - len(marker), 0)
    head = room // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def safe_ename(name: str | None) -> str:
    """An exception class name fit to appear as structure, or ``Error``."""
    if name and _ENAME.match(name):
        return name
    return "Error"


def display_name(name: str | None) -> str:
    """A display name as printable single-line text of bounded length."""
    if not name:
        return ""
    cleaned = "".join(
        ch if unicodedata.category(ch)[0] not in ("C", "Z") or ch == " " else " " for ch in name
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_NAME:
        cleaned = cleaned[: _MAX_NAME - 1] + "…"
    return cleaned


__all__ = [
    "UNKNOWN_AUTHOR",
    "clip",
    "display_name",
    "safe_ename",
    "wrap",
    "wrap_text",
]
