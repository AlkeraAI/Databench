"""Blob reference extraction — the shape walk that roots a handle.

GC runs one walk per persisted line at every sweep and re-derives the root set
each time, so an under-rooting bug repairs at the next sweep instead of
freezing into logs already on disk. A handle is recognized by SHAPE, never by
parent key, because rooting by key name lost results the chat could still page.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def collect_blob_references(obj: object) -> set[str]:
    """Every blob sha referenced anywhere in a JSON-shaped object. Over-rooting
    only keeps bytes; under-rooting loses a result the chat can still page."""
    out: set[str] = set()
    _walk(obj, out)
    return out


def _walk(obj: object, out: set[str]) -> None:
    # An explicit stack, not recursion: a pathologically nested line (a bash
    # result, machine-generated data) can be deeper than the interpreter's
    # frame limit, and a blown walk would abort the whole sweep.
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            _add_dict_refs(cur, out)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str):
            if _SHA256_HEX.fullmatch(cur):
                out.add(cur)
            else:
                _collect_from_encoded(cur, out)


def _add_dict_refs(obj: dict[str, Any], out: set[str]) -> None:
    """Any dict carrying a 64-hex ``sha256`` (or ``handle``, the ``references``
    spelling) is a blob reference, whatever key its producer nested it under —
    ``blob``, ``result_blob``, a ``references`` entry, a file part, a tool-call
    input."""
    for key in ("sha256", "handle"):
        value = obj.get(key)
        if isinstance(value, str) and _SHA256_HEX.fullmatch(value):
            out.add(value)


def _collect_from_encoded(text: str, out: set[str]) -> None:
    """A string value that parses as JSON gets the same walk, because the harness
    adapters serialize whole tool results into a string ``output`` where a spilled
    handle can sit. The fast-reject spares the parse only for payloads that cannot
    spell a sha, meaning no raw hex run and no ``\\u`` escape. A legal encoder may
    write the hex digits as escapes, so the raw scan alone is not proof of absence."""
    head = text[:1]
    if head.isspace():
        head = text.lstrip()[:1]
    if head not in ("{", "["):
        return
    if not _SHA256_HEX.search(text) and "\\u" not in text:
        return
    try:
        _walk(json.loads(text), out)
    except (ValueError, RecursionError):
        # JSON nested past the parser's limit still raises. Root every hex
        # match instead of losing a handle the chat can still page;
        # over-rooting only keeps bytes.
        out.update(_SHA256_HEX.findall(text))


__all__ = ["collect_blob_references"]
