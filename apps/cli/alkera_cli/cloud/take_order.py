"""The order a box takes the chats it was listed, and which of them it may
leave untaken.

A chat that owes a turn (a message nobody answered, a reader looking at it)
comes first, then one written to in the last hour, then the rest. Read off the
listed chat rows alone, so the order is a pure function of the listing and the
clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

#: A chat written to within this long is taken before the idle ones.
RECENT_ACTIVITY_SECONDS = 3600.0


def take_rank(chat: Mapping[str, Any], *, now: datetime) -> int:
    """0 for a chat that owes a turn, 1 for one active in the last hour, 2 for
    the rest."""
    if somebody_waits(chat):
        return 0
    idle = untouched_for(chat, now=now)
    return 1 if idle is not None and idle <= RECENT_ACTIVITY_SECONDS else 2


def left_asleep(chat: Mapping[str, Any]) -> bool:
    """Whether the row says a box put this chat to sleep and nothing has come
    for it since: no reader opened it and nothing waits in it. Read only for a
    chat the box holds no memory of; while it runs, its own record of the
    chats it released is that memory."""
    return chat.get("machine_status") == "asleep" and not somebody_waits(chat)


def somebody_waits(chat: Mapping[str, Any]) -> bool:
    """A message nobody answered, or a reader looking at the chat: a standing
    wake is the backend's word that somebody opened it, cleared the moment a
    box reports the chat awake."""
    return bool(chat.get("pending_turn")) or bool(chat.get("wake_requested_at"))


def untouched_for(chat: Mapping[str, Any], *, now: datetime) -> float | None:
    """Seconds since the row's last activity, or ``None`` when the row does
    not say (an older server, or a stamp that does not parse)."""
    raw = chat.get("last_activity_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now - at).total_seconds()


def in_order_of_need(
    chats: Mapping[str, dict[str, Any]], *, now: datetime
) -> list[tuple[str, dict[str, Any]]]:
    """The listed chats in the order a box takes them: those that owe a turn
    (a message nobody answered, a turn a restart cut short), then those
    active in the last hour, then the rest — each group in listing order. A
    box coming up over hundreds of chats takes one in five to ten seconds, so
    the order is the difference between the person waiting being answered
    first and being answered minutes later."""
    return sorted(chats.items(), key=lambda item: take_rank(item[1], now=now))
