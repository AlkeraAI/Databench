"""`.alkera/` directory management.

A `ProjectDirectory` owns the per-workspace `.alkera/` directory tree.
It exposes sub-managers as scoped handles — `chats()` returns a
`ChatStore`, etc. No global state is shared between sub-manager
handles; each is cheap to construct.

The lock primitive (`FileLock`) lives here too. The atomic write helpers
live in the leaf module `alkera_core.atomic_io` and are re-exported here.

Platform note: stale-lock reclaim is currently POSIX-only. Windows
raises `NotImplementedError` on the reclaim path; manual `.lock`
removal is the workaround. See `locking.py` for details.
"""

from __future__ import annotations

from alkera_core.atomic_io import (
    append_line,
    append_line_safe,
    append_lines,
    append_lines_safe,
    write_bytes_atomic,
    write_json_atomic,
    write_text_atomic,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.jsonl import iter_jsonl, tail_jsonl
from alkera_core.project.local_state import (
    is_local_state,
    local_state_patterns,
    register_local_state,
)
from alkera_core.project.locking import FileLock, LockHeldError, acquire, prune_stale_locks

__all__ = [
    "FileLock",
    "LockHeldError",
    "ProjectDirectory",
    "acquire",
    "append_line",
    "append_line_safe",
    "append_lines",
    "append_lines_safe",
    "is_local_state",
    "iter_jsonl",
    "local_state_patterns",
    "prune_stale_locks",
    "register_local_state",
    "tail_jsonl",
    "write_bytes_atomic",
    "write_json_atomic",
    "write_text_atomic",
]
