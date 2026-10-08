"""Small rules the live plane reads the drive's answers and paths by.

Pure functions of their arguments, kept apart from the live sync so each can
be read (and tested) on its own: which node a conflict answer names, a count
an answer carries, whether a reply's text names a path, whether the tree route
would file a path at all, which entries a refused batch names, what wait a
refusal asks for and what wrote an item's head.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx
from alkera_core.project.local_state import is_local_state

from alkera_cli.files.name_rules import path_refusal
from alkera_cli.files.push import _is_pointer

__all__ = [
    "answer_count",
    "answer_node",
    "head_source",
    "mentions",
    "refused_paths",
    "stamp",
    "tree_safe",
]


class _Pathed(Protocol):
    @property
    def path(self) -> str: ...


def answer_node(answer: Mapping[str, Any]) -> str | None:
    """The copy's node id, under whichever name the answer carries it."""
    for key in ("nodeId", "node_id", "resultNodeId"):
        value = answer.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def answer_count(answer: Mapping[str, Any], *keys: str) -> int:
    """The first of ``keys`` the answer carries as a number, else zero."""
    for key in keys:
        value = answer.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def mentions(text: str, relative: str, base: str) -> bool:
    """Whether ``text`` names ``relative`` as a whole path, or its absolute form
    under ``base`` (``notes.md`` is not named by ``old-notes.md``)."""
    for spelled in (f"{base.rstrip('/')}/{relative}", relative):
        start = text.find(spelled)
        while start != -1:
            before = text[start - 1] if start > 0 else ""
            after = text[start + len(spelled) : start + len(spelled) + 1]
            beyond = text[start + len(spelled) + 1 : start + len(spelled) + 2]
            # One character each side decides it; an empty one is the text's
            # edge, which joins nothing (and ``"" in "_-"`` would say it does).
            joined_before = bool(before) and (before.isalnum() or before in "_-.")
            joined_after = bool(after) and (
                after.isalnum() or after in "_-" or (after == "." and beyond.isalnum())
            )
            if not joined_before and not joined_after:
                return True
            start = text.find(spelled, start + 1)
    return False


def stamp(path: Path) -> tuple[int, int] | None:
    """``(size, mtime_ns)`` of ``path`` as the disk holds it now, or ``None``."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def tree_safe(relative: str) -> bool:
    """Whether the tree route would file ``relative`` under the folder at all.

    The route's own rule, asked before a batch leaves: no empty, ``.`` or
    ``..`` segment, no leading ``/``, no name it refuses (``path_refusal``), no
    local state, no pointer. One path it refuses costs the whole batch.
    """
    if not relative or relative.startswith("/"):
        return False
    if any(part in ("", ".", "..") for part in relative.split("/")):
        return False
    if is_local_state(relative) or path_refusal(relative) is not None:
        return False
    return not _is_pointer(relative.encode("utf-8", "surrogateescape"))


def refused_paths(refusal: BaseException, chunk: Sequence[_Pathed]) -> set[str]:
    """The entries a mismatch refusal names, when its body names any.

    The positions in ``indexes`` are exact, so when the route names any they
    are the whole answer. ``paths`` is read only when it names none: the
    route spells a path from the leased folder, this holder from the watched
    directory inside it, so a path can only be matched by its tail -- and a
    tail matches every entry that ends the same way. ``bad dir/readme.md``
    refused must not drop the ``readme.md`` beside it.
    """
    response = getattr(refusal, "response", None)
    if not isinstance(response, httpx.Response) or not response.content:
        return set()
    try:
        body: Any = response.json()
    except ValueError:
        return set()
    # The error envelope's details, else the flat body's detail a server older
    # than the envelope answered with.
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        detail = error.get("details")
    else:
        detail = body.get("detail") if isinstance(body, Mapping) else None
    if not isinstance(detail, Mapping):
        return set()
    named: set[str] = set()
    indexes = detail.get("indexes")
    if isinstance(indexes, list):
        named.update(
            chunk[index].path
            for index in indexes
            if isinstance(index, int) and 0 <= index < len(chunk)
        )
    if named:
        return named
    paths = detail.get("paths")
    if isinstance(paths, list):
        spelled = {path for path in paths if isinstance(path, str)}
        named.update(
            entry.path
            for entry in chunk
            if entry.path in spelled or any(path.endswith(f"/{entry.path}") for path in spelled)
        )
    return named


def head_source(item: Mapping[str, Any]) -> str | None:
    """What wrote ``item``'s head version, when the drive says."""
    facet = item.get("file")
    metadata = facet.get("metadata") if isinstance(facet, Mapping) else None
    source = metadata.get("head_source") if isinstance(metadata, Mapping) else None
    return source if isinstance(source, str) else None
