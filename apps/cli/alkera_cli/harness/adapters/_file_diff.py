"""Unified-diff shaping shared by the harness adapters.

A write/edit tool gates on a permission ask, and the UI wants to show the
proposed change on the card BEFORE the user decides. opencode hands us a
ready-made unified diff in the permission metadata; the Claude adapter has only
the tool input, so it builds the diff here from the old + new file text. Either
way the diff is reduced to the same `(insertions, deletions, preview)` shape the
`PermissionRequest` and `FileEdited` events carry, so both harnesses render
identically.

Pure functions, no I/O — the caller reads the file; this only shapes text.
"""

from __future__ import annotations

import difflib
from typing import Any


def count_diff_lines(diff: str) -> tuple[int, int]:
    """Added/removed body lines in a unified diff, excluding the `+++`/`---`
    file headers. A new-file diff is all-additions."""
    insertions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1
    return insertions, deletions


def diff_preview(path: str, diff: str) -> tuple[int, int, dict[str, Any]]:
    """`(insertions, deletions, {kind, content, title})` for a unified diff —
    the renderable preview the permission ask shows before approval and the
    `FileEdited` carries after the write."""
    insertions, deletions = count_diff_lines(diff)
    basename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # `path` is carried so a consumer can match the preview to its write card by
    # path when the tool-call id spaces differ across harnesses.
    return insertions, deletions, {"kind": "diff", "content": diff, "title": basename, "path": path}


def unified_diff_for(old: str, new: str, path: str) -> str:
    """A unified diff from `old` to `new`, labelled with `path` on both sides.
    A fresh create (``old == ""``) yields an all-additions diff whose hunk header
    reads ``@@ -0,0...`` — the zero original-line marker a renderer reads as
    "this file is being created". Matches opencode's `createTwoFilesPatch` shape
    (minus its `Index:` preamble, which renderers drop as boilerplate)."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


__all__ = ["diff_preview", "unified_diff_for"]
