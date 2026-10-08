"""Who is told, on every replica, that a chat's transcript moved.

Every path that writes a ``chat_messages`` row -- a person's message from any
surface, the machine's published events, a recorded answer, a stop -- commits
a ``doc.op`` outbox row for ``doc:chat:<chat_id>`` in the same transaction. The
outbox listener publishes every committed row into this process's hub, on
every replica, only after the commit. So "the transcript of chat X may have a
new entry" is one hub predicate, and this module is the one consumer of it: a
service that must react to transcript growth registers a :data:`TranscriptWatcher`
here instead of polling or hooking one surface's write path.

A watcher is a plain synchronous callable taking the chat id, or ``None`` when
this process may have missed events (the hub dropped a burst for a slow
consumer): ``None`` means "anything may have moved; re-check everything". It
must be cheap and must not raise -- it runs on the consumer task, and the usual
body is "note the id, set an event". A watcher that raises is logged and the
others still run.

Registered callers today: the product's Slack relay, which wakes its sweeper
so a reply reaches a linked thread within one pass of reaching the web,
whichever surface started the turn. Anything else that has to follow a chat's
transcript -- a notification fan-out, an email digest, another
chat surface -- registers the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from uuid import UUID

from alkera_core.events import EventHub
from alkera_core.events.hub import HubEvent, ResetMarker
from alkera_core.events.types import EventType
from alkera_core.logging import get_logger

log = get_logger(__name__)

#: ``chat_id`` of the transcript that moved, or ``None`` for "maybe any".
TranscriptWatcher = Callable[[UUID | None], None]

_CHANNEL_PREFIX = "doc:chat:"

_WATCHERS: dict[str, TranscriptWatcher] = {}


def register(name: str, watcher: TranscriptWatcher) -> None:
    """Register (or replace) the watcher called ``name``."""
    _WATCHERS[name] = watcher


def unregister(name: str) -> None:
    """Remove the watcher called ``name``. Idempotent."""
    _WATCHERS.pop(name, None)


def registered() -> tuple[str, ...]:
    """The names of the watchers registered now."""
    return tuple(_WATCHERS)


def chat_of(event: HubEvent) -> UUID | None:
    """The chat whose transcript a hub event says moved, or ``None``."""
    if event.lane != "durable" or event.type != EventType.DOC_OP.value:
        return None
    entity = event.entity_id or ""
    if not entity.startswith(_CHANNEL_PREFIX):
        return None
    try:
        return UUID(entity[len(_CHANNEL_PREFIX) :])
    except ValueError:
        return None


def notify(chat_id: UUID | None) -> None:
    """Tell every registered watcher that ``chat_id`` moved."""
    for name, watcher in list(_WATCHERS.items()):
        try:
            watcher(chat_id)
        except Exception as exc:  # one watcher's bug must not starve the rest
            log.warning("transcript_watch.watcher_failed", watcher=name, error=type(exc).__name__)


async def watch(hub: EventHub) -> None:
    """Feed the registered watchers from ``hub`` until cancelled."""
    sub = hub.subscribe(lambda event: chat_of(event) is not None, label="transcript-watch")
    try:
        while True:
            item = await sub.queue.get()
            if isinstance(item, ResetMarker):
                hub.ack_reset(sub)
                notify(None)
                continue
            notify(chat_of(item))
    finally:
        hub.unsubscribe(sub)


def start(hub: EventHub) -> asyncio.Task[None]:
    """Run :func:`watch` on ``hub`` as a task the caller owns."""
    return asyncio.create_task(watch(hub), name="alkera-transcript-watch")


async def stop(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


__all__ = [
    "TranscriptWatcher",
    "chat_of",
    "notify",
    "register",
    "registered",
    "start",
    "stop",
    "unregister",
    "watch",
]
