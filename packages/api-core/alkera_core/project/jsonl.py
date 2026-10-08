"""Crash-safe JSONL iteration.

Append-only writers fsync after every line, but a process killed
mid-write (SIGKILL / power loss) can still leave a truncated trailing
line. The reader's job is to skip that one line and recover the rest.

We don't try to recover from corruption *inside* a line — if a line is
malformed JSON in the middle of the file, we log + skip it. The caller
can choose to surface that as a warning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one decoded JSON object per line in ``path``.

    Crash-safety contract:
    - If the file is missing, yields nothing.
    - If the final line is partial (no trailing newline AND fails to
      parse), it's silently skipped — that's the only "the writer
      crashed" recovery path.
    - Internal malformed lines (somewhere in the middle of the file)
      ARE skipped, but the reader doesn't surface a warning at this
      layer; chat-level callers wrap with their own logging.

    The function streams — it doesn't load the whole file in memory.
    """
    try:
        f = path.open("rb")
    except FileNotFoundError:
        return
    with f:
        # We read line-by-line to be memory-cheap on big logs. Lines
        # may include the trailing newline depending on the iterator;
        # strip it before json.loads.
        for raw in f:
            stripped = raw.rstrip(b"\r\n")
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, RecursionError):
                # A line nested past the parser's frame budget is as unusable as
                # corruption, and letting it escape aborts every read of this file.
                # Could be the partial last line (no terminating \n) or
                # corruption mid-file. We can detect the former because
                # `raw` won't end with \n when it's the final partial
                # line; that's the canonical "skip this and move on".
                if not raw.endswith(b"\n"):
                    # Truncated tail — done.
                    return
                # Mid-file malformed line — skip silently. The caller's
                # higher-level fold step is the right place to detect +
                # report this; we don't have context here.
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                # JSONL convention requires one object per line. A
                # bare array/string/number would still parse; we skip
                # for safety so downstream type assumptions hold.
                continue


def tail_jsonl(path: Path, *, n: int) -> list[dict[str, Any]]:
    """Return the last ``n`` valid JSON objects from ``path``.

    Reads the whole file (we don't have a seek-from-end fast path
    today; can be added if it shows up in profiles). For chats of
    thousands of events this is still microseconds.
    """
    out: list[dict[str, Any]] = []
    for obj in iter_jsonl(path):
        out.append(obj)
        if len(out) > n:
            out.pop(0)
    return out


__all__ = ["iter_jsonl", "tail_jsonl"]
