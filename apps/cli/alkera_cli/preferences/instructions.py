"""Persisted GLOBAL agent instructions — `~/.alkera/instructions.md`.

A plain Markdown file the user maintains once and the agent applies to EVERY
project (alongside per-repo `ALKERA.md`/`AGENTS.md`/`CLAUDE.md`). Edited from the
VS Code preferences panel through the daemon (`instructions.get`/`instructions.set`)
and directly on disk; both the CLI and the daemon write it, so:

- `load_global_instructions()` is a lock-free best-effort read — `write_text_atomic`
  swaps the file via rename, so a reader sees the old or new file whole, never torn.
  A missing / unreadable file degrades to `""` (empty editor, no instructions).
- `save_global_instructions()` holds a `FileLock` (`~/.alkera/.instructions.lock`)
  for the write so a concurrent writer can't interleave.

It is plain text, NOT a `VersionedModel`: it's prompt content, not a structured
schema. Injection caps its size (`cap_global_instructions`) so a runaway file can't
dominate the context window / cost — the editor still stores the full content.
"""

from __future__ import annotations

import time

from alkera_core.project import FileLock, LockHeldError, write_text_atomic

from alkera_cli.host import paths
from alkera_cli.host.backoff import retry_until

_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_FIRST_POLL_SECONDS = 0.02
_LOCK_MAX_POLL_SECONDS = 0.2

# Conservative per-injection cap, matching the vendored opencode instruction cap
# (32KB/file). Normal instruction files never hit it — it's a guard, not a budget.
MAX_GLOBAL_INSTRUCTIONS_BYTES = 32 * 1024


def load_global_instructions() -> str:
    """Return the stored global instructions, or `""` if the file is missing /
    unreadable. Never raises. Returns the FULL content (the editor shows it all;
    the size cap is applied only at injection time)."""
    path = paths.INSTRUCTIONS_FILE_PATH
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def save_global_instructions(content: str) -> str:
    """Atomically persist the global instructions under the file lock. Returns the
    stored content (the input verbatim). An empty string is a valid value — it
    clears the instructions (the file is written empty, not deleted)."""
    paths.ensure_home()
    with _InstructionsLock():
        write_text_atomic(paths.INSTRUCTIONS_FILE_PATH, content, mode=0o600)
    return content


def cap_global_instructions(content: str, limit: int = MAX_GLOBAL_INSTRUCTIONS_BYTES) -> str:
    """Head-truncate `content` to `limit` bytes (UTF-8) with a marker, mirroring the
    vendored opencode per-file cap. Returns `content` unchanged when within budget."""
    encoded = content.encode("utf-8")
    if len(encoded) <= limit:
        return content
    head = encoded[:limit].decode("utf-8", errors="ignore")
    shown = round(limit / 1024)
    total = round(len(encoded) / 1024)
    return (
        f"{head}\n\n[... global instructions truncated: showing first {shown}KB of {total}KB ...]"
    )


# --- internals -------------------------------------------------------------


class _InstructionsLock:
    """Acquire the instructions `FileLock` with a short bounded retry (mirrors
    `preferences_file._PreferencesLock`)."""

    def __init__(self) -> None:
        self._lock = FileLock(paths.INSTRUCTIONS_LOCK_PATH)

    def __enter__(self) -> FileLock:
        retry_until(
            self._lock.acquire,
            retry_on=LockHeldError,
            timeout=_LOCK_TIMEOUT_SECONDS,
            first=_LOCK_FIRST_POLL_SECONDS,
            cap=_LOCK_MAX_POLL_SECONDS,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        return self._lock

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


__all__ = [
    "MAX_GLOBAL_INSTRUCTIONS_BYTES",
    "cap_global_instructions",
    "load_global_instructions",
    "save_global_instructions",
]
