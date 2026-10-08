"""The live-sync watcher: :func:`alkera_cli.files.tree_watch.watch_tree` over
the chat's working directory, with the filter that keeps this machine's own
records out of it.

A name that is not UTF-8 arrives as a surrogate-escaped path like any other;
the live sync refuses it for that file alone (the drive's JSON wire cannot
carry it, see :mod:`alkera_cli.files.name_rules`) and goes on with the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from alkera_core.chat_records import CHAT_RECORD_NAMES
from alkera_core.project.local_state import is_local_state

from alkera_cli.files.tree_watch import Change, watch_tree

if TYPE_CHECKING:
    from alkera_cli.files.live_sync import LiveCadence

#: Whether the disk is read on a timer rather than listened to. macOS answers
#: yes: its event stream drops a file creation outright about two runs in five
#: under the pattern a turn makes — a subprocess writing into a directory the
#: watch was armed on before it started — and puts no ceiling on the lag of the
#: ones it does deliver. A live push that misses a write misses it silently:
#: the file is on the box, the turn ends, and the reader is never told it
#: exists. Linux boxes keep inotify, which does not drop, and pay nothing.
POLL_THE_DISK: Final = sys.platform == "darwin"


def live_watch_filter(change: Change, path: str) -> bool:
    """Whether the watcher hands ``path`` to the classifier at all.

    The write lock and the forensic rotations a reclaim leaves beside it are
    this machine's hold on the folder, not the chat's work, and a lock file
    rewritten on every open would wake a flush a second on an idle chat.
    """
    del change
    parts = Path(path).parts
    name = parts[-1] if parts else ""
    if name in CHAT_RECORD_NAMES or ".runtime" in parts:
        return False
    return not is_local_state(name)


class TreeWatcher:
    """The production watcher over the chat's working directory.

    The watched root is the working directory rather than the chat folder, so
    the records beside it are outside the walk by construction — the tree
    enforces that rather than a filter having to catch every rotation of them.
    """

    def __init__(self, root: Path, *, cadence: LiveCadence, stop: Any | None = None) -> None:
        self.root = root
        self.cadence = cadence
        self.stop = stop

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        return watch_tree(
            self.root,
            watch_filter=live_watch_filter,
            debounce_ms=self.cadence.debounce_ms,
            step_ms=max(self.cadence.debounce_ms // 10, 1),
            stop_event=self.stop,
            # The loop flushes when a batch arrives and the batch window has
            # passed, so a watcher that only speaks when the disk moves leaves
            # the LAST write of a turn queued until something else happens —
            # which for a chat that has just finished writing its report is
            # "never". Ticking on the batch cadence turns that into one empty
            # round the loop hands straight to the flush.
            tick_ms=max(self.cadence.batch_every_ms, 1),
            force_polling=POLL_THE_DISK,
            # A poll costs a walk, so it is taken at the rate a round leaves
            # here rather than faster: a change found sooner than that waits
            # for the batch window anyway.
            poll_delay_ms=max(self.cadence.batch_every_ms, 1),
        )


async def wake_on(
    stream: AsyncIterator[set[tuple[Change, str]]], due_in: Callable[[], float | None]
) -> AsyncIterator[set[tuple[Change, str]]]:
    """The batches of ``stream``, and an empty one whenever ``due_in()``
    seconds pass before the next arrives (``None``: no wake is owed).

    A watcher speaks when the disk moves and ticks on the batch window, so a
    loop that owes a look sooner (a file held back until it settles) would
    otherwise wait for the tick. The next batch is asked for only once the
    last one was handled, as a plain ``async for`` would, and a wake never
    cancels that request: the same read is waited on again after it.
    """
    pending: asyncio.Future[set[tuple[Change, str]]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(stream))
            wait = due_in()
            done, _ = await asyncio.wait(
                {pending}, timeout=None if wait is None else max(wait, 0.01)
            )
            if not done:
                yield set()
                continue
            arrived, pending = pending, None
            try:
                batch = arrived.result()
            except StopAsyncIteration:
                return
            yield batch
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


__all__ = [
    "POLL_THE_DISK",
    "TreeWatcher",
    "live_watch_filter",
    "wake_on",
]
