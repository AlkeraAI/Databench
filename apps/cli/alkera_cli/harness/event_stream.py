"""``ChatEventStream``: a chat's events, history then live, from one source.

A chat's history lives in its ``chat.jsonl``; its live tail comes from the
in-process ``EventBus`` of the open ``ChatSession``. This class yields both as
one stream, de-duplicated by ``event_id`` so the overlap never double-counts. It
never takes the per-chat write lock, so an observer (a user inspecting a running
subagent, the spawn card streaming its tool calls) can follow a chat the parent
is driving.

- :meth:`for_session`: a live, in-process session. Snapshot its persisted
  prefix and subscribe to its bus.
- :meth:`replay`: a chat not open in this runtime. Its persisted events only,
  read lock-free.

The daemon's ``open_chat`` and ``observe_chat`` both read through this class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING

from alkera_core.schemas.chat import Event

if TYPE_CHECKING:
    from alkera_cli.harness.runtime import ChatSession


class ChatEventStream:
    """History-then-live events for one chat, de-duped by ``event_id``."""

    def __init__(self, history: list[Event], live: AsyncIterator[Event] | None) -> None:
        self._history = history
        self._live = live

    @classmethod
    def for_session(cls, session: ChatSession) -> ChatEventStream:
        """Live source. Subscribe to the bus BEFORE snapshotting the persisted
        prefix, so no event slips through the gap between the two; the resulting
        snapshot/live overlap is de-duped by ``event_id`` in :meth:`live`. Takes no
        new lock — the session already holds it."""
        live = session.subscribe()
        history = list(session.events())
        return cls(history, live)

    @classmethod
    def replay(cls, events: Iterable[Event]) -> ChatEventStream:
        """Replay source — a chat's persisted events only (read lock-free), with no
        live tail. For a chat that is not open in this runtime."""
        return cls(list(events), None)

    @property
    def history(self) -> list[Event]:
        """The persisted prefix (the replay), in append order."""
        return self._history

    async def live(self) -> AsyncIterator[Event]:
        """New events after the history snapshot, de-duped against it by
        ``event_id`` (the snapshot/subscribe overlap). Yields nothing for a
        replay-only stream."""
        if self._live is None:
            return
        seen = {event.event_id for event in self._history if event.event_id}
        async for event in self._live:
            eid = event.event_id
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            yield event


__all__ = ["ChatEventStream"]
