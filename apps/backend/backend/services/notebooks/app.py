"""The notebook service an application runs, and its realtime pieces.

The realtime runtime builds one :class:`NotebookService` at start, bound to
its hub (the event feed and the caret board follow the hub's publish) and to
its CRDT lane's notebook facade. A process without a runtime (the OpenAPI
export, an in-process test client) gets one bound to nothing: its routes
still decide and record, and a test may install its own on
``app.state.notebook_service``.
"""

from __future__ import annotations

from typing import Any, Final, cast

from alkera_core.events import EventHub, HubEvent
from alkera_core.events.listener import EPHEMERAL_CHANNEL, ephemeral_payload
from sqlalchemy import text

from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookDocs, NotebookService, RecordedDecider
from backend.services.notebooks.transport import MachineChannelTransport

#: Where the realtime runtime is kept (``application.state``); the realtime
#: package spells it too, and a test pins that the two agree.
RUNTIME_ATTR: Final = "realtime"
#: Where a test (or the runtime-less app) keeps its service.
STATE_ATTR: Final = "notebook_service"


async def publish_ephemeral(event: HubEvent) -> None:
    """Fan ``event`` out to every replica on the ephemeral lane, in its own
    transaction."""
    from alkera_core.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL synchronous_commit = off"))
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": EPHEMERAL_CHANNEL, "payload": ephemeral_payload(event)},
        )
        await db.commit()


def build_service(hub: EventHub | None, docs: Any, *, decide: RecordedDecider) -> NotebookService:
    """A service following ``hub``, writing through ``docs`` (the CRDT lane's
    notebook facade, ``None`` when the lane serves no notebooks) and deciding
    a channel's widget messages with ``decide`` (``decide_on_record``, which
    only the layers above the services may import)."""
    feed = NotebookFeed(hub)
    feed.start()
    carets = CaretBoard(hub)
    carets.start()
    return NotebookService(
        transport=MachineChannelTransport(),
        feed=feed,
        carets=carets,
        decide=decide,
        docs=cast("NotebookDocs | None", docs),
        publish_ephemeral=publish_ephemeral,
    )


def service_for(application: Any, *, decide: RecordedDecider) -> NotebookService:
    """The application's notebook service: a test's, the runtime's, or one
    bound to nothing (deciding with ``decide``)."""
    installed = getattr(application.state, STATE_ATTR, None)
    if isinstance(installed, NotebookService):
        return installed
    runtime = getattr(application.state, RUNTIME_ATTR, None)
    found = getattr(runtime, "notebooks", None)
    if isinstance(found, NotebookService):
        return found
    service = build_service(None, None, decide=decide)
    setattr(application.state, STATE_ATTR, service)
    return service


async def close_service(service: NotebookService | None) -> None:
    if service is None:
        return
    await service.close()
    await service.feed.close()
    service.carets.close()


__all__ = [
    "RUNTIME_ATTR",
    "STATE_ATTR",
    "build_service",
    "close_service",
    "publish_ephemeral",
    "service_for",
]
