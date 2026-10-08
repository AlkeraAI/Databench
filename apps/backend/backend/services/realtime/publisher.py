"""Where the box publishing a chat stands, told to the chat's readers.

A killed box goes unnoticed for most of a minute when the page waits on the
machine's heartbeat, which turns only after the ready window lapses. The box's
socket is the first sign there is, so the gateway tells every reader of the
chats a box publishes when the box comes onto a chat's channel (``here``) and
when its socket goes (``gone``), as a ``publisher`` frame carrying the
server's clock. A drain sends no ``gone``: the box reconnects to another
replica within moments. The box itself is never sent its own news.

It is a hint, so it never costs a socket its work: a failure to say it is
logged and the page falls back to the heartbeat.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.logging import get_logger
from alkera_core.schemas.realtime import PublisherFrame, PublisherState
from pydantic import ValidationError

from backend.services.realtime import presence
from backend.services.realtime.channels import ChannelGrant

log = get_logger(__name__)


def publisher_frame(event: HubEvent, *, machine_id: str | None) -> PublisherFrame | None:
    """The frame a ``doc.publisher`` event becomes on a reader's socket, or
    ``None`` on a box's (it knows where it is) or for a malformed event."""
    if machine_id is not None:
        return None
    try:
        return PublisherFrame.model_validate(
            {
                "channel": str(event.channel),
                "state": event.payload.get("state"),
                "at": event.payload.get("at"),
            }
        )
    except ValidationError:
        log.warning("realtime.ws.bad_publisher", channel=event.channel)
        return None


def chat_grants(grants: Iterable[object]) -> list[ChannelGrant]:
    """The chat channels among a socket's grants. A box publishes chats only;
    a workspace channel (or any other document) has no publisher to report."""
    return [g for g in grants if isinstance(g, ChannelGrant) and g.channel.doc_type == "chat"]


async def announce(grants: list[ChannelGrant], state: PublisherState) -> None:
    """Tell every reader of these chats where their publisher stands."""
    if not grants:
        return
    at = datetime.now(UTC).isoformat()
    try:
        async with AsyncSessionLocal() as db:
            for grant in grants:
                await presence.publish_ephemeral(
                    db,
                    presence.ephemeral_event(
                        grant=grant,
                        type=presence.PUBLISHER_EVENT_TYPE,
                        body={"state": state, "at": at},
                    ),
                )
            await db.commit()
    except Exception as exc:  # a lost hint must not end the socket
        log.warning(
            "realtime.ws.publisher_announce_failed",
            state=state,
            error=f"{type(exc).__name__}: {exc}",
        )


async def subscribed(machine_id: str | None, grant: object) -> None:
    """A socket took a channel: a box on a chat's channel is its publisher."""
    if machine_id is not None:
        await announce(chat_grants([grant]), "here")


async def socket_ended(machine_id: str | None, draining: bool, grants: Iterable[object]) -> None:
    """A socket ended: a box's chats lost their publisher, unless the server
    is draining and the box is about to reconnect elsewhere."""
    if machine_id is not None and not draining:
        await announce(chat_grants(grants), "gone")
