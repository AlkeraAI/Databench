"""Cells whose re-run waits for someone to stop editing them.

An autorun never re-runs a descendant somebody is editing. Such a cell is
*held*: it is stale, says whose editing it waits for, and is looked at again
until that editing ends. Then the session re-runs it (autorun) or leaves it
stale (lazy). A hold ends without a re-run when the cell runs, its text
changes, it is deleted or the kernel goes away: each of those already says
what the cell needs next.

This is the one owner of that set. The session tells it what to hold and
what to drop; it asks the store who is editing and calls back when a hold
ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from alkera_notebook.document.editing import blocker
from alkera_notebook.document.store import EditingInfo
from alkera_notebook.engine.models import Actor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hold:
    #: Who the cell waits for, by display name.
    who: str
    #: Who asked for the run that skipped it: the re-run is theirs.
    requester: Actor


class HeldReruns:
    def __init__(
        self,
        *,
        editing: Callable[[], Awaitable[Mapping[str, Sequence[EditingInfo]]]],
        release: Callable[[str, Actor], Awaitable[None]],
        changed: Callable[[str], None],
        sleep: Callable[[float], Awaitable[None]],
        interval_s: float,
    ) -> None:
        self._editing = editing
        self._release = release
        self._changed = changed
        self._sleep = sleep
        self._interval_s = interval_s
        self._held: dict[str, Hold] = {}
        self._watch: asyncio.Task[None] | None = None

    def who(self, cell_id: str) -> str | None:
        hold = self._held.get(cell_id)
        return None if hold is None else hold.who

    def hold(self, cell_id: str, who: str, requester: Actor) -> None:
        self._held[cell_id] = Hold(who, requester)
        self._changed(cell_id)
        if self._watch is None or self._watch.done():
            self._watch = asyncio.get_running_loop().create_task(self._watching())

    def drop(self, cell_id: str) -> None:
        if self._held.pop(cell_id, None) is not None:
            self._changed(cell_id)

    def drop_all(self) -> None:
        for cell_id in list(self._held):
            self.drop(cell_id)

    async def _watching(self) -> None:
        while self._held:
            await self._sleep(self._interval_s)
            try:
                now = await self._editing()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Who is editing could not be read: every hold stands.
                log.exception("held re-runs: reading who is editing failed")
                continue
            for cell_id, hold in list(self._held.items()):
                if self._held.get(cell_id) is not hold:
                    continue
                still = blocker(now.get(cell_id, ()), hold.requester.id)
                if still is not None:
                    if still.display_name != hold.who:
                        self._held[cell_id] = Hold(still.display_name, hold.requester)
                        self._changed(cell_id)
                    continue
                del self._held[cell_id]
                self._changed(cell_id)
                try:
                    await self._release(cell_id, hold.requester)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("held re-runs: re-running %s failed", cell_id)

    async def close(self) -> None:
        self._held.clear()
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watch
