"""A directory watch that never follows a link and never stops the process.

``watchfiles`` was the watch here before, and two of its properties are wrong
for a tree an agent writes. Its Rust core arms a recursive watch by walking the
tree *following symbolic links*, and it does that walk inside the constructor
while holding the interpreter lock. One ``ln -s / root`` in a chat folder turned
arming the watch into a walk of the whole machine with every other thread of
the daemon frozen behind it: no heartbeat, no lease renewal, no log line, until
somebody restarted the box — and the next take of that folder froze it again.
It also refuses a name that is not UTF-8 by ending the watch, dropping every
other change of that batch with it.

This watch walks with ``os.scandir`` and never follows a link (the kernel watch
is armed with ``IN_DONT_FOLLOW`` too), does its walking and its waiting on a
worker thread in plain Python — which gives the interpreter lock back between
system calls — and spells every name with ``os.fsdecode``, so a name that is
not UTF-8 arrives as a surrogate-escaped ``str`` like any other path and the
caller decides what to do with it.

Linux listens with inotify; elsewhere, or where inotify cannot be had (the
watch limit is exhausted, the call is missing), the tree is polled. Batches are
shaped the way the ``watchfiles`` watch shaped them: a batch closes once a step
passes with nothing new, or once ``debounce_ms`` has passed since its first
change, and an optional tick hands the caller an empty batch when nothing moved.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import functools
import logging
import os
import select
import stat
import struct
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Final, Protocol

import anyio.to_thread

logger = logging.getLogger(__name__)

__all__ = [
    "Change",
    "PollSource",
    "WatchFilter",
    "inotify_available",
    "open_source",
    "watch_tree",
]


class Change(IntEnum):
    """What happened to a path in a batch. The values are the ones the
    ``watchfiles`` enum this replaced used, so a number anything stored reads
    back as the same change."""

    added = 1
    modified = 2
    deleted = 3


#: ``(change, path) -> keep?`` — the same callable shape ``watchfiles`` took.
WatchFilter = Callable[[Change, str], bool]

Event = tuple[Change, str]


class _Stoppable(Protocol):
    def is_set(self) -> bool: ...


class Source(Protocol):
    """Where changes come from. Every method blocks; none holds a lock long."""

    def arm(self) -> None:
        """Put the watch on the tree. May walk it; never follows a link."""

    def read(self, timeout: float) -> list[Event]:
        """The changes seen since the last read, waiting at most ``timeout``."""

    def close(self) -> None:
        """Release the watch. Safe from any thread, and more than once."""


def _descend(path: str, watch_filter: WatchFilter | None) -> bool:
    """Whether a directory is worth watching under.

    A directory the filter refuses as an addition is one whose every child the
    filter refuses too (a ``.git``, a ``node_modules``, an agent's runtime
    state), so watching beneath it would only spend watches and walk time on
    events the caller throws away.
    """
    return watch_filter is None or watch_filter(Change.added, path)


def _walk_dirs(
    root: str,
    watch_filter: WatchFilter | None,
    closed: threading.Event,
    before: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, list[os.DirEntry[str]]]]:
    """Every directory under ``root`` (itself first) with its entries.

    ``os.scandir`` with no link ever followed: a link to ``/`` is one entry,
    not a second filesystem, and a loop of links is two entries. A directory
    that cannot be read (gone, refused) is skipped without ending the walk.
    ``before`` runs on each directory ahead of its listing (a watch armed then
    hears whatever the listing misses) and answers whether to list it at all.
    """
    pending = [root]
    while pending and not closed.is_set():
        directory = pending.pop()
        if before is not None and not before(directory):
            continue
        try:
            with os.scandir(directory) as listing:
                entries = list(listing)
        except OSError:
            continue
        yield directory, entries
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir and _descend(entry.path, watch_filter):
                pending.append(entry.path)


# -- polling ------------------------------------------------------------------

_Signature = tuple[int, int, int, int]


def _signature(entry: os.DirEntry[str]) -> _Signature | None:
    try:
        found = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_size, found.st_ino, found.st_mode)


class PollSource:
    """The tree read on a timer and compared with the last reading.

    The fallback wherever the kernel will not listen, and the choice on macOS,
    whose event stream drops creations under the pattern a turn makes. A
    directory's own modification time is not reported: what changed inside it
    is, path by path.
    """

    def __init__(
        self,
        roots: Sequence[str],
        *,
        watch_filter: WatchFilter | None,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._roots = list(roots)
        self._filter = watch_filter
        self._interval = max(interval, 0.01)
        self._clock = clock
        self._closed = threading.Event()
        self._seen: dict[str, _Signature] = {}
        self._next = 0.0

    def _snapshot(self) -> dict[str, _Signature]:
        seen: dict[str, _Signature] = {}
        for root in self._roots:
            for _directory, entries in _walk_dirs(root, self._filter, self._closed):
                for entry in entries:
                    signature = _signature(entry)
                    if signature is not None:
                        seen[entry.path] = signature
        return seen

    def arm(self) -> None:
        self._seen = self._snapshot()
        self._next = self._clock() + self._interval

    def read(self, timeout: float) -> list[Event]:
        wait = self._next - self._clock()
        if wait > timeout:
            self._closed.wait(max(timeout, 0.0))
            return []
        if wait > 0 and self._closed.wait(wait):
            return []
        if self._closed.is_set():
            return []
        now = self._snapshot()
        self._next = self._clock() + self._interval
        before, self._seen = self._seen, now
        changes: list[Event] = []
        for path, signature in now.items():
            was = before.get(path)
            if was is None:
                changes.append((Change.added, path))
            elif was != signature and not stat.S_ISDIR(signature[3]):
                changes.append((Change.modified, path))
        changes.extend((Change.deleted, path) for path in before.keys() - now.keys())
        return changes

    def close(self) -> None:
        self._closed.set()


# -- inotify ------------------------------------------------------------------

IN_MODIFY: Final = 0x00000002
IN_ATTRIB: Final = 0x00000004
IN_CLOSE_WRITE: Final = 0x00000008
IN_MOVED_FROM: Final = 0x00000040
IN_MOVED_TO: Final = 0x00000080
IN_CREATE: Final = 0x00000100
IN_DELETE: Final = 0x00000200
IN_DELETE_SELF: Final = 0x00000400
IN_MOVE_SELF: Final = 0x00000800
IN_Q_OVERFLOW: Final = 0x00004000
IN_IGNORED: Final = 0x00008000
IN_ONLYDIR: Final = 0x01000000
IN_DONT_FOLLOW: Final = 0x02000000
IN_EXCL_UNLINK: Final = 0x04000000
IN_ISDIR: Final = 0x40000000

_WATCH_MASK: Final = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_ONLYDIR
    | IN_DONT_FOLLOW
    | IN_EXCL_UNLINK
)

_EVENT_HEADER: Final = struct.Struct("iIII")
_READ_BYTES: Final = 256 * 1024


class _WatchLimitError(OSError):
    """The kernel will not give this user another watch."""


def _libc() -> ctypes.CDLL | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.inotify_rm_watch.restype = ctypes.c_int
    except (OSError, AttributeError):
        return None
    return libc


_LIBC: Final = _libc()


def inotify_available() -> bool:
    """Whether this host can listen with inotify at all."""
    return _LIBC is not None


class InotifySource:
    """The kernel's inotify, one watch per directory, armed without following.

    A directory that appears (made, or moved in) is armed and walked, and what
    is already in it is reported as added: a file written into a new directory
    before its watch was up has no event of its own. A queue overflow re-arms
    the whole tree and reports everything as added, which a caller that
    compares with what it already sent settles cheaply.
    """

    def __init__(self, roots: Sequence[str], *, watch_filter: WatchFilter | None) -> None:
        if _LIBC is None:
            raise OSError(errno.ENOSYS, "inotify is not available")
        self._libc = _LIBC
        self._roots = list(roots)
        self._filter = watch_filter
        self._closed = threading.Event()
        fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        self._fd = fd
        self._wake_r, self._wake_w = os.pipe()
        self._by_wd: dict[int, str] = {}
        self._by_path: dict[str, int] = {}
        #: Only one thread reads; ``close`` waits for it so the descriptors are
        #: never closed under a read that is still selecting on them.
        self._reading = threading.Lock()
        self._fds_closed = False
        #: The kernel ran out of watches for a directory that appeared after
        #: arming: part of the tree is no longer heard, so the caller should
        #: move this watch to a poll.
        self.degraded = False

    # The watch table ------------------------------------------------------

    def _add(self, directory: str) -> bool:
        # A root the caller named through a link is the directory it chose;
        # only what is found beneath it is never followed.
        mask = _WATCH_MASK & ~IN_DONT_FOLLOW if directory in self._roots else _WATCH_MASK
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(directory), mask)
        if wd < 0:
            code = ctypes.get_errno()
            if code == errno.ENOSPC:
                raise _WatchLimitError(code, "the inotify watch limit is reached")
            return False
        stale = self._by_wd.get(wd)
        if stale is not None and stale != directory:
            self._by_path.pop(stale, None)
        self._by_wd[wd] = directory
        self._by_path[directory] = wd
        return True

    def _arm_under(self, root: str, *, report: bool) -> list[Event]:
        found: list[Event] = []
        for _directory, entries in _walk_dirs(root, self._filter, self._closed, self._add):
            if report:
                found.extend((Change.added, entry.path) for entry in entries)
        return found

    def _arm_later(self, root: str) -> list[Event]:
        """Arm a directory that appeared while reading; a full table degrades
        the watch instead of ending it."""
        try:
            return self._arm_under(root, report=True)
        except _WatchLimitError:
            self.degraded = True
            return [(Change.added, root)]

    def _forget_under(self, directory: str) -> None:
        prefix = directory + os.sep
        for path in [p for p in self._by_path if p == directory or p.startswith(prefix)]:
            wd = self._by_path.pop(path)
            self._by_wd.pop(wd, None)
            self._libc.inotify_rm_watch(self._fd, wd)

    def arm(self) -> None:
        for root in self._roots:
            self._arm_under(root, report=False)

    # Reading ------------------------------------------------------------------

    def read(self, timeout: float) -> list[Event]:
        with self._reading:
            if self._closed.is_set():
                return []
            ready, _, _ = select.select([self._fd, self._wake_r], [], [], max(timeout, 0.0))
            if self._fd not in ready or self._closed.is_set():
                return []
            try:
                raw = os.read(self._fd, _READ_BYTES)
            except BlockingIOError:
                return []
            return self._parse(raw)

    def _parse(self, raw: bytes) -> list[Event]:
        changes: list[Event] = []
        offset = 0
        while offset + _EVENT_HEADER.size <= len(raw):
            wd, mask, _cookie, length = _EVENT_HEADER.unpack_from(raw, offset)
            offset += _EVENT_HEADER.size
            name = raw[offset : offset + length].rstrip(b"\0")
            offset += length
            if mask & IN_Q_OVERFLOW:
                changes.extend(self._rearm_all())
                continue
            directory = self._by_wd.get(wd)
            if mask & IN_IGNORED:
                if directory is not None:
                    self._by_wd.pop(wd, None)
                    if self._by_path.get(directory) == wd:
                        del self._by_path[directory]
                continue
            if directory is None:
                continue
            if not name:
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF) and directory in self._roots:
                    changes.append((Change.deleted, directory))
                continue
            path = os.path.join(directory, os.fsdecode(name))
            changes.extend(self._translate(mask, path))
        return changes

    def _translate(self, mask: int, path: str) -> list[Event]:
        is_dir = bool(mask & IN_ISDIR)
        if mask & (IN_CREATE | IN_MOVED_TO):
            found: list[Event] = [(Change.added, path)]
            if is_dir and _descend(path, self._filter):
                found.extend(self._arm_later(path))
            return found
        if mask & (IN_DELETE | IN_MOVED_FROM):
            if is_dir:
                self._forget_under(path)
            return [(Change.deleted, path)]
        if mask & (IN_MODIFY | IN_CLOSE_WRITE | IN_ATTRIB) and not is_dir:
            return [(Change.modified, path)]
        return []

    def _rearm_all(self) -> list[Event]:
        for wd in list(self._by_wd):
            self._libc.inotify_rm_watch(self._fd, wd)
        self._by_wd.clear()
        self._by_path.clear()
        found: list[Event] = []
        for root in self._roots:
            found.extend(self._arm_later(root))
        return found

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")
        with self._reading:
            if self._fds_closed:
                return
            self._fds_closed = True
            for fd in (self._fd, self._wake_r, self._wake_w):
                with contextlib.suppress(OSError):
                    os.close(fd)


# -- choosing a source ----------------------------------------------------------


def _polling(
    roots: Sequence[str], *, watch_filter: WatchFilter | None, poll_interval: float
) -> PollSource:
    polling = PollSource(roots, watch_filter=watch_filter, interval=poll_interval)
    polling.arm()
    return polling


def open_source(
    roots: Sequence[str],
    *,
    watch_filter: WatchFilter | None,
    force_polling: bool,
    poll_interval: float,
) -> Source:
    """An armed source for ``roots``: inotify when it can be had, else a poll.

    Arming walks the tree, so this blocks; call it off the event loop. A watch
    limit reached part-way falls back to polling for the whole watch rather
    than leaving a subtree unwatched.
    """
    if not force_polling and inotify_available():
        try:
            listening = InotifySource(roots, watch_filter=watch_filter)
        except OSError as unavailable:
            logger.warning("inotify is unavailable (%s); polling %s instead", unavailable, roots)
        else:
            try:
                listening.arm()
            except _WatchLimitError:
                listening.close()
                logger.warning(
                    "the inotify watch limit was reached arming %s; polling it instead", roots
                )
            else:
                return listening
    return _polling(roots, watch_filter=watch_filter, poll_interval=poll_interval)


async def watch_tree(
    *roots: Path | str,
    watch_filter: WatchFilter | None = None,
    debounce_ms: int = 1600,
    step_ms: int = 50,
    tick_ms: int = 0,
    stop_event: _Stoppable | None = None,
    force_polling: bool = False,
    poll_delay_ms: int = 300,
    source_factory: Callable[[], Source] | None = None,
) -> AsyncIterator[set[Event]]:
    """Batches of changes under ``roots`` until ``stop_event`` is set.

    ``tick_ms`` above zero yields an empty batch whenever that long passes with
    nothing seen, so a caller with timed work has a turn to do it. The waiting
    happens on the loop's worker threads (anyio's, the allowance every other
    thread hop of the loop shares) in bounded slices: a cancelled caller is let
    go, and its thread given back, within one slice, and the event loop is
    never the one blocked.
    """
    spelled = [os.fspath(root) for root in roots]

    def build() -> Source:
        if source_factory is not None:
            return source_factory()
        return open_source(
            spelled,
            watch_filter=watch_filter,
            force_polling=force_polling,
            poll_interval=poll_delay_ms / 1000,
        )

    def stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    source = await anyio.to_thread.run_sync(build)
    try:
        step = max(step_ms, 1) / 1000
        debounce = max(debounce_ms, 0) / 1000
        tick = tick_ms / 1000 if tick_ms > 0 else None
        idle_slice = min(tick, 1.0) if tick is not None else 1.0
        while not stopped():
            batch: set[Event] = set()
            quiet_since = time.monotonic()
            first_at: float | None = None
            last_size = 0
            while True:
                wait = step if batch else idle_slice
                for change, path in await anyio.to_thread.run_sync(source.read, wait):
                    if watch_filter is None or watch_filter(change, path):
                        batch.add((change, path))
                if getattr(source, "degraded", False):
                    logger.warning(
                        "the inotify watch limit was reached under %s; polling it instead",
                        spelled,
                    )
                    source.close()
                    source = await anyio.to_thread.run_sync(
                        functools.partial(
                            _polling,
                            spelled,
                            watch_filter=watch_filter,
                            poll_interval=poll_delay_ms / 1000,
                        )
                    )
                if stopped():
                    return
                now = time.monotonic()
                if batch:
                    if first_at is None:
                        first_at = now
                    elif len(batch) == last_size or now - first_at >= debounce:
                        break
                    last_size = len(batch)
                elif tick is not None and now - quiet_since >= tick:
                    break
            yield batch
    finally:
        source.close()
