"""Keeping a working copy current while it is open: when to sync, never how.

:class:`~alkera_cli.files.working_copy.WorkingCopy` decides what one pass does.
This module decides when a pass runs, from three signals:

* **the drive said so**: a realtime frame naming a file the copy knows reads
  just that file back. A file being edited live in a browser is written back
  every couple of seconds, and reading the whole tree for each write-back is a
  storm. A frame naming anything else the copy knows (a new node in one of its
  folders, the chat folder's lease) asks for a full pass, held a little longer
  so a burst costs one. A reconnected stream, or the server's ``reset``, means
  frames may have been missed, so it asks for a full pass too;
* **the disk said so**: a filesystem event under the copy. It first asks the
  copy the cheap question (has anything on disk moved away from the last
  agreement?) so the copy's own downloads, which raise events too, cost no
  request, and then only sends what changed;
* **somebody asked**: an editor save (sends, reads nothing back) and a slow
  periodic full pass that catches anything every other signal missed.

Passes never overlap: the copy is one directory, and two passes interleaving
their writes would each decide on a disk the other is changing.

The copies an owner (the daemon's JSON-RPC server) keeps attached live here
too, in :func:`sessions_of`, so the daemon method layer only translates calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar

from alkera_core.files import PULL_PART_SUFFIX

from alkera_cli.files.tree_watch import watch_tree
from alkera_cli.files.working_copy import (
    CopyStoppedError,
    Keep,
    SyncReport,
    WorkingCopy,
    WorkingCopyAuthError,
    concerns,
)

__all__ = [
    "STREAM_OPENED",
    "AttachedCopy",
    "Backoff",
    "FrameSource",
    "WatchSource",
    "WorkingCopyRunner",
    "WorkingCopySessions",
    "sessions_of",
    "watch_directory",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The marker a frame source yields once its stream is up, before any frame:
#: the realtime client's own (``alkera_cli.cloud.rest.STREAM_OPENED``), spelled
#: here as a value so this library does not import the box's client.
STREAM_OPENED: Final = "alkera.stream.opened"

#: Frames that say "you may have missed something; look at everything".
_LOOK_AGAIN: Final = frozenset({STREAM_OPENED, "reset"})

#: Opens one realtime stream. It ends or raises when the stream drops, and the
#: runner opens another after a pause.
FrameSource = Callable[[], AsyncIterator[Mapping[str, Any]]]

#: Watches a directory, yielding once per batch of filesystem changes.
WatchSource = Callable[[Path], AsyncIterator[object]]

#: How long the runner waits after the first signal before it syncs, so a save
#: that writes three files (or a burst of frames) costs one pass.
DEBOUNCE_SECONDS: Final = 0.3

#: How much longer a full pass waits, so frames arriving together (a folder of
#: new files, a box taking the chat) cost one read of the tree.
FULL_DEBOUNCE_SECONDS: Final = 1.5

#: A pass this often whatever else happens. The realtime stream can drop a
#: frame (see the realtime README's known limits); this bounds how long a
#: missed one can leave the copy behind.
POLL_SECONDS: Final = 300.0


class Backoff(Protocol):
    """The reconnect schedule for a dropped stream. The daemon passes the one
    the box's own event stream uses (``alkera_cli.host.backoff``), so a fleet
    of editors reconnecting after an outage spreads out the same way."""

    def next_delay(self) -> float: ...

    def connected(self) -> None: ...

    def decay(self) -> int: ...


class WorkingCopyRunner:
    """Runs one copy's passes until stopped.

    ``on_report`` hears every pass that did something or has something to say
    (a conflict, a refusal); ``on_error`` hears a pass that failed outright.
    Both are where the daemon turns a pass into a notification.
    """

    def __init__(
        self,
        copy: WorkingCopy,
        *,
        frames: FrameSource | None,
        watch: WatchSource | None,
        on_report: Callable[[SyncReport], Awaitable[None]],
        on_error: Callable[[BaseException], Awaitable[None]],
        debounce: float = DEBOUNCE_SECONDS,
        full_debounce: float = FULL_DEBOUNCE_SECONDS,
        backoff: Backoff,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.copy = copy
        self._frames = frames
        self._watch = watch
        self._on_report = on_report
        self._on_error = on_error
        self._debounce = debounce
        self._full_debounce = full_debounce
        self._poll = poll_seconds
        self._backoff = backoff
        self._wanted = asyncio.Event()
        self._full = False
        self._push = False
        self._refresh: set[str] = set()
        self._exclusive = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start listening. Idempotent."""
        if self._tasks:
            return
        loops: list[Awaitable[None]] = [self._passes(), self._ticks()]
        if self._frames is not None:
            loops.append(self._listen())
        if self._watch is not None:
            loops.append(self._watch_disk())
        self._tasks = [asyncio.ensure_future(loop) for loop in loops]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def _halt(self) -> None:
        """Stop every loop but the one calling, which returns on its own."""
        current = asyncio.current_task()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if task is not current:
                task.cancel()

    # ---- asking for a pass -----------------------------------------------

    def request(self) -> None:
        """Ask for a full pass: everything sent, the whole tree read back."""
        self._full = True
        self._wanted.set()

    def request_push(self) -> None:
        """The disk may have changed: send what did, read nothing back."""
        self._push = True
        self._wanted.set()

    def request_refresh(self, paths: frozenset[str]) -> None:
        """Read just these known files back from the drive."""
        self._refresh.update(paths)
        self._wanted.set()

    def set_dirty(self, paths: frozenset[str]) -> None:
        """The editor's unsaved files. A file that stops being dirty without a
        save (a revert) is read back, since it was held while dirty."""
        cleaned = self.copy.set_dirty(paths)
        if cleaned:
            self.request_refresh(cleaned)

    async def sync_now(self, *, pull: bool = True) -> SyncReport:
        """One pass now, after any pass already running."""
        return await self._run(lambda: self.copy.sync(pull=pull))

    async def resolve(self, path: str, keep: Keep) -> SyncReport:
        return await self._run(lambda: self.copy.resolve(path, keep))

    async def resolve_deletions(self, *, apply: bool) -> SyncReport:
        return await self._run(lambda: self.copy.resolve_deletions(apply=apply))

    async def _run(self, work: Callable[[], T]) -> T:
        async with self._exclusive:
            return await asyncio.to_thread(work)

    # ---- the loops --------------------------------------------------------

    async def _passes(self) -> None:
        while True:
            await self._wanted.wait()
            await asyncio.sleep(self._debounce)
            if self._full:
                await asyncio.sleep(max(self._full_debounce - self._debounce, 0))
            self._wanted.clear()
            full, self._full = self._full, False
            push, self._push = self._push, False
            refresh, self._refresh = frozenset(self._refresh), set()
            try:
                report = await self._one_pass(full=full, push=push, refresh=refresh)
            except asyncio.CancelledError:
                raise
            except CopyStoppedError as exc:
                # Nothing more to sync, and nothing to retry: stop listening and
                # leave the local files exactly as they are.
                await self._on_error(exc)
                self._halt()
                return
            except Exception as exc:  # reported, then the next signal tries again
                logger.warning("working_copy.pass_failed", exc_info=True)
                await self._on_error(exc)
                continue
            if report is None:
                continue
            if report.needs_full:
                self.request()
            if report.changed or report.needs_answer:
                await self._on_report(report)

    async def _one_pass(
        self, *, full: bool, push: bool, refresh: frozenset[str]
    ) -> SyncReport | None:
        """The cheapest pass that answers what was asked for."""
        if full:
            return await self.sync_now()
        report: SyncReport | None = None
        if push and await self._run(self.copy.local_changed):
            report = await self.sync_now(pull=False)
        if refresh:
            fetched = await self._run(lambda: self.copy.refresh(refresh))
            report = fetched if report is None else _merged(report, fetched)
        return report

    async def _ticks(self) -> None:
        while True:
            await asyncio.sleep(self._poll)
            self.request()

    async def _listen(self) -> None:
        assert self._frames is not None
        while True:
            try:
                async for frame in self._frames():
                    kind = frame.get("type")
                    if kind == STREAM_OPENED:
                        self._backoff.connected()
                    self._backoff.decay()
                    named = _named_files(self.copy, frame)
                    if named:
                        self.request_refresh(named)
                    elif kind in _LOOK_AGAIN or concerns(self.copy.record, frame):
                        self.request()
            except asyncio.CancelledError:
                raise
            except WorkingCopyAuthError as exc:
                await self._on_error(exc)
            except Exception:
                logger.info("working_copy.stream_dropped", exc_info=True)
            await asyncio.sleep(self._backoff.next_delay())

    async def _watch_disk(self) -> None:
        assert self._watch is not None
        async for _batch in self._watch(self.copy.root):
            self.request_push()


def _named_files(copy: WorkingCopy, frame: Mapping[str, Any]) -> frozenset[str]:
    """The known files a node frame is about, when it is about one."""
    data = frame.get("data")
    if frame.get("type") != "file_node.changed" or not isinstance(data, Mapping):
        return frozenset()
    entity = data.get("entity_id")
    return copy.paths_of_node(entity) if isinstance(entity, str) and entity else frozenset()


def _merged(first: SyncReport, second: SyncReport) -> SyncReport:
    """Two passes' reports as one."""
    return SyncReport(
        downloaded=first.downloaded + second.downloaded,
        uploaded=first.uploaded + second.uploaded,
        removed=first.removed + second.removed,
        trashed=first.trashed + second.trashed,
        restored=first.restored + second.restored,
        pending=first.pending + second.pending,
        conflicts=first.conflicts + second.conflicts,
        refused=first.refused + second.refused,
        busy=first.busy or second.busy,
        needs_full=first.needs_full or second.needs_full,
    )


def _copy_watch_filter(change: object, path: str) -> bool:
    """A pull's resume sidecar is the copy's own scratch, not a change."""
    del change
    return not path.endswith(PULL_PART_SUFFIX.decode("ascii"))


async def watch_directory(root: Path) -> AsyncIterator[object]:
    """The production :data:`WatchSource`: a link-blind watch over the copy."""
    async for batch in watch_tree(root, watch_filter=_copy_watch_filter):
        yield batch


# ---- the copies one owner keeps current -------------------------------------


@dataclass
class AttachedCopy:
    """One attached copy: its runner, and how to let go of what it holds."""

    runner: WorkingCopyRunner
    close: Callable[[], None]


class WorkingCopySessions:
    """The copies one owner (a daemon's JSON-RPC server) keeps current.

    Keyed by the copy's resolved root. A stop asked for from inside a runner
    (its own pass hit a refused sign-in or a lost tree) cannot await the stop
    that cancels it, so it runs as a task held here until it finishes; a task
    nobody holds can be collected mid-flight and its failure is lost.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AttachedCopy] = {}
        self._stopping: set[asyncio.Task[None]] = set()
        self._attaching: dict[str, asyncio.Lock] = {}

    def attach_lock(self, root: str) -> asyncio.Lock:
        """Serializes attaches of one root: the editor attaches on every
        daemon start and every sign-in, and two attaches racing through the
        get-then-start would leave a second runner nobody stops."""
        return self._attaching.setdefault(_key(root), asyncio.Lock())

    def get(self, root: str) -> AttachedCopy | None:
        return self._sessions.get(_key(root))

    def put(self, root: str, session: AttachedCopy) -> None:
        self._sessions[_key(root)] = session

    async def stop(self, root: str) -> None:
        session = self._sessions.pop(_key(root), None)
        if session is None:
            return
        await session.runner.stop()
        session.close()

    def stop_later(self, root: str) -> None:
        task = asyncio.get_running_loop().create_task(self.stop(root))
        self._stopping.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._stopping.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("working_copy.stop_failed", exc_info=task.exception())

    async def stop_all(self) -> None:
        for key in list(self._sessions):
            await self.stop(key)
        if self._stopping:
            await asyncio.gather(*self._stopping, return_exceptions=True)


#: Each owner's sessions. Weakly keyed, so an owner (a test's server, an
#: embedder's) takes its copies with it when it goes, and two never share one.
_BY_SERVER: weakref.WeakKeyDictionary[object, WorkingCopySessions] = weakref.WeakKeyDictionary()


def sessions_of(server: object) -> WorkingCopySessions:
    """The working-copy sessions ``server`` owns."""
    found = _BY_SERVER.get(server)
    if found is None:
        found = _BY_SERVER[server] = WorkingCopySessions()
    return found


def _key(root: str) -> str:
    return os.path.realpath(root)
