"""Crash-safe write helpers for `.alkera/` files and other small state files.

This module imports only the standard library, so a process that must stay
small (the box supervisor) can write a file safely without loading the
project store.

Pattern: write to a sibling temp file, fsync, then `os.replace` —
which is atomic on POSIX and on Windows for files on the same volume.
Readers always observe either the old file or the new file, never a
half-written one.

Why we don't use `tempfile.NamedTemporaryFile`: it creates in the
system temp dir by default, and a cross-device `os.replace` raises
`OSError`. We always create the temp as a sibling so the rename is
guaranteed to be on the same filesystem.
"""

from __future__ import annotations

import contextlib
import json
import os
import random as _random
import secrets
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final

#: Windows refuses to rename over — or delete — a file while any handle is open
#: on it, and raises `PermissionError` (ERROR_ACCESS_DENIED and
#: ERROR_SHARING_VIOLATION both surface as errno 13). The handle can belong to a
#: concurrent reader (CPython's `open()` asks for no delete-sharing, so one is
#: enough), a peer writer mid-rename onto the same target, or a virus scanner
#: that grabbed the temp we just closed. Every one of those clears in
#: milliseconds, so the operation is retried on a full-jitter exponential
#: backoff — twelve attempts, roughly one and a half seconds of patience on
#: average and three at worst. The jitter matters as much as the budget: an
#: equal pause re-synchronises a herd that collided once into colliding again on
#: every retry, which is how sixteen concurrent savers exhausted a fixed
#: five-times-50ms schedule. Harmless on POSIX, where rename and unlink ignore
#: open handles entirely.
_SHARING_ATTEMPTS = 12
_SHARING_BACKOFF_S = 0.01
_SHARING_BACKOFF_CAP_S = 0.4

#: The mode Alkera writes its own state with: the owner alone reads it. A box
#: daemon runs every org's chats as root beside agents that run as other uids,
#: and on a laptop the state holds what the person's other local users have no
#: business reading, so private is the default and anything wider is asked for.
PRIVATE_FILE_MODE: Final = 0o600

#: Returns a fraction in [0, 1) scaling one backoff pause. Injectable so a test
#: asserts the schedule instead of sleeping through a random one.
Jitter = Callable[[], float]


def _with_sharing_retry(attempt: Callable[[], None], *, jitter: Jitter) -> None:
    """Run ``attempt`` until it stops raising ``PermissionError``, pausing on a
    full-jitter exponential backoff between tries and re-raising once the budget
    is spent. The one place the sharing-violation schedule is spelled, so the
    rename and the delete ride out the same window."""
    for number in range(1, _SHARING_ATTEMPTS + 1):
        try:
            attempt()
            return
        except PermissionError:
            if number == _SHARING_ATTEMPTS:
                raise
            ceiling = min(_SHARING_BACKOFF_CAP_S, _SHARING_BACKOFF_S * 2.0 ** (number - 1))
            time.sleep(jitter() * ceiling)


def replace_with_retry(tmp: Path, path: Path, *, jitter: Jitter = _random.random) -> None:
    """``os.replace`` with a jittered, bounded retry on sharing violations.

    Atomic same-volume on POSIX and Windows; on Windows it can raise
    ``PermissionError`` while another handle is open on either path — ride that
    out, then let the final failure propagate (the caller sweeps the temp).
    Shared by the blob store and the lock-reclaim path, not just the atomic
    writers here."""
    _with_sharing_retry(lambda: os.replace(tmp, path), jitter=jitter)


def unlink_with_retry(path: Path, *, jitter: Jitter = _random.random) -> None:
    """``os.unlink`` with the same jittered, bounded retry as `replace_with_retry`.

    A reader that merely has the file open blocks the delete on Windows, so a
    delete contended by pollers needs the same patience a rename does. A path
    that is already gone counts as deleted; a refusal that outlasts the budget
    propagates, because a caller that believes it deleted a file it did not is
    how a released lock goes on reading as held."""

    def _attempt() -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return

    _with_sharing_retry(_attempt, jitter=jitter)


def write_bytes_atomic(path: Path, data: bytes, *, mode: int | None = PRIVATE_FILE_MODE) -> None:
    """Atomic write of ``data`` to ``path``.

    Writes to ``<path>.<rand>.tmp`` in the same directory, fsyncs the
    file (and its parent directory), then ``os.replace``s into place.
    If anything raises before the replace, the temp file is left
    on-disk; callers can sweep `*.tmp` files at startup if desired.

    ``mode`` is what the file ends up with whatever the process umask:
    :data:`PRIVATE_FILE_MODE` unless the caller asks for another. ``None``
    leaves it to the umask, as any file the person makes, for a file that is
    the person's own work written into their project (one they may commit),
    never for Alkera's state.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # `secrets.token_hex(4)` produces 8 hex chars — short, unique enough.
    tmp = parent / f"{path.name}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # Don't leak the temp file on early-write failure. Re-raise.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        if mode is not None:
            os.chmod(tmp, mode)
        replace_with_retry(tmp, path)
        # Best-effort fsync of the parent dir so the rename is durable
        # across power-loss. Not all FS support it; ignore errors — and on
        # Windows you can't open a directory as a fd at all, so this is a no-op.
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        # If `os.replace` itself failed, the temp is orphaned — clean it. The
        # cleanup can itself be refused (Windows, while a scanner still holds the
        # temp); a failed sweep must never replace the error that actually
        # stopped the write, and `sweep_stale_temps` reclaims the leftover later.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, text: str, *, mode: int | None = PRIVATE_FILE_MODE) -> None:
    """UTF-8 atomic write."""
    write_bytes_atomic(path, text.encode("utf-8"), mode=mode)


def write_json_atomic(path: Path, payload: Any, *, mode: int | None = PRIVATE_FILE_MODE) -> None:
    """JSON-encode + atomic write. Pretty-printed for human inspection
    (manifests etc. are not hot paths)."""
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    write_text_atomic(path, text, mode=mode)


def append_line(path: Path, line: str) -> None:
    """Append a single line to a file, ensuring a trailing newline.

    Used for JSONL writes. Single-process atomicity is provided by the
    per-chat write lock — this function does NOT lock; callers must
    hold the chat's `FileLock` before invoking.

    We open + write + fsync + close on every call. That's slower than
    keeping a long-lived handle, but it makes crash recovery trivial:
    a partial write only loses the in-flight line, and the JSONL
    reader's `iter_jsonl` skips a truncated trailing line cleanly.
    """
    append_lines(path, [line])


def append_lines(path: Path, lines: Iterable[str]) -> None:
    """Append several lines with one open, one write and one fsync.

    The durability is `append_line`'s — every line is on disk when this
    returns — at one flush per batch instead of one per line, which is the
    difference between a trace stream that absorbs a burst and one that spends
    forty milliseconds per event on NTFS. A batch cut short by a crash loses a
    tail the reader skips, as before. Nothing to append opens nothing. A
    file this makes is :data:`PRIVATE_FILE_MODE`; one that exists keeps its
    mode.
    """
    payload = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    if not payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "ab") as f:
            f.write(payload.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # `os.fdopen` may have already taken ownership of fd; only close
        # explicitly on the failure path where it didn't.
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def sweep_stale_temps(directory: Path, *, older_than_seconds: float = 3600.0) -> int:
    """Delete orphaned ``*.tmp`` siblings left by a crashed atomic write, but only
    those older than ``older_than_seconds`` so a LIVE concurrent writer's temp is
    never reaped. Returns the count removed. Best-effort — never raises."""
    removed = 0
    cutoff = time.time() - older_than_seconds
    try:
        entries = list(directory.glob("*.tmp"))
    except OSError:
        return 0
    for tmp in entries:
        try:
            if tmp.is_file() and tmp.stat().st_mtime < cutoff:
                tmp.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def append_line_safe(path: Path, line: str) -> None:
    """Append a line, first closing any torn previous tail.

    If a prior writer was killed mid-line the file ends without a newline;
    terminate that fragment with a bare newline before appending, so the new
    line starts clean and `iter_jsonl` drops the lone fragment. Does not lock;
    callers hold the relevant `FileLock`.
    """
    _terminate_dangling_line(path)
    append_line(path, line)


def append_lines_safe(path: Path, lines: Iterable[str]) -> None:
    """`append_lines`, first closing any torn previous tail (see `append_line_safe`)."""
    _terminate_dangling_line(path)
    append_lines(path, lines)


def _terminate_dangling_line(path: Path) -> None:
    """If `path` ends mid-line (no trailing newline), close that line."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size == 0:
        return
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return
    append_line(path, "")


__all__ = [
    "PRIVATE_FILE_MODE",
    "Jitter",
    "append_line",
    "append_line_safe",
    "replace_with_retry",
    "sweep_stale_temps",
    "unlink_with_retry",
    "write_bytes_atomic",
    "write_json_atomic",
    "write_text_atomic",
]
