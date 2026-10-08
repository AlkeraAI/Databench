"""The text of the event stream: frame encoders and the cursor parser.

Pure functions over plain data so the wire format is unit-tested apart from
the connection that carries it. The format is ``text/event-stream``: a frame is
a block of ``field: value`` lines ended by a blank line; a line starting with a
colon is a comment the client ignores (the keepalive); ``retry:`` sets the
client's reconnect delay; ``id:`` sets the cursor the client sends back as
``Last-Event-ID`` when it reconnects.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from alkera_core.events import HubEvent, RealtimeEventType
from alkera_core.schemas.realtime import SseEventData, SseResetData

#: The reconnect delay a client is told at open (two seconds: fast enough that
#: a rolling deploy is a blip, slow enough that a fleet-wide restart does not
#: land every client back in the same instant).
RETRY_MS = 2000
#: The delay hinted when the server closes a stream at its deadline: the client
#: comes straight back, cursor in hand.
RETRY_AFTER_DEADLINE_MS = 1000
CONNECTED_COMMENT = ": connected\n\n"
KEEPALIVE_COMMENT = ": keepalive\n\n"
RESET_OVERFLOW = "overflow"
RESET_CURSOR_AHEAD = "cursor_ahead"
#: The cursor is so far behind that replaying would be unbounded work.
RESET_CURSOR_TOO_OLD = "cursor_too_old"
ERROR_UNAUTHORIZED = "unauthorized"
#: How many framed outbox ids one stream remembers so the same row is never
#: framed twice. Outbox ids are not framed in order — a row that committed
#: after a higher id was already read arrives late, by its own id — so the
#: dedupe is a set of recent ids rather than a high-water mark. The window
#: only has to outlast the gap between a catch-up read and the hub delivering
#: the same row; the same number the listener remembers is far more than that.
FRAMED_ID_MEMORY = 4096


class FramedIds:
    """The outbox ids a stream has framed, bounded to the most recent
    :data:`FRAMED_ID_MEMORY`; the oldest is forgotten first."""

    __slots__ = ("_ids", "_limit")

    def __init__(self, seed: Iterable[int] = (), *, limit: int = FRAMED_ID_MEMORY) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._ids: OrderedDict[int, None] = OrderedDict()
        for id_ in seed:
            self.add(id_)

    def add(self, id_: int) -> bool:
        """Remember ``id_``; ``False`` when it was already remembered (a
        duplicate the caller must not frame again)."""
        if id_ in self._ids:
            return False
        self._ids[id_] = None
        while len(self._ids) > self._limit:
            self._ids.popitem(last=False)
        return True

    def __contains__(self, id_: object) -> bool:
        return id_ in self._ids

    def __len__(self) -> int:
        return len(self._ids)


def retry_frame(milliseconds: int) -> str:
    return f"retry: {milliseconds}\n\n"


def cursor_frame(id_: int) -> str:
    """An ``id:`` line with no ``data:``. A client does not dispatch it as an
    event but does take it as its resume cursor, so it is sent right after a
    frame whose own id is below the cursor (a straggler) to put the cursor back
    where it was — a reconnect then replays only what came after."""
    return f"id: {id_}\n\n"


#: The payload keys a frame may carry beyond the entity it names, and the only
#: ones. Every value is an id (or, for ``reason``, a short word the writer
#: chose) that a reader holding the entity id could have asked for anyway; the
#: list is spelled here rather than derived from the payload so a writer that
#: starts putting something richer on an outbox row can never widen the wire by
#: accident. Anything else on the payload stays off the stream.
NARROWING_KEYS: tuple[str, ...] = ("drive_id", "parent_id", "reason", "lease_node_id")
#: The flags a frame may carry beyond them, each only ever ``True``: a word
#: the writer set about how wide the change was, never data about it.
NARROWING_FLAGS: tuple[str, ...] = ("subtree",)


def event_frame(event: HubEvent) -> str:
    """One invalidation frame. The ``data:`` body is thin on purpose — what
    changed and where, never the change (see ``SseEventData``)."""
    if event.id is None:
        raise ValueError("an event frame needs an outbox id to resume from")
    body = SseEventData(
        # A server-only type (``doc.op``, ``authz.decision``) is a ValueError
        # here: the predicate never admits one, and a frame must not be forged.
        type=RealtimeEventType(event.type),
        entity=event.entity,
        entity_id=event.entity_id,
        version=event.version,
        org_id=event.org_id,
        **_narrowing(event),
    )
    # ``exclude_none`` so a writer that held none of them costs the wire
    # nothing: the client reads an absent key and an explicit null the same way.
    dumped = body.model_dump_json(exclude_none=True)
    return f"id: {event.id}\nevent: {event.type}\ndata: {dumped}\n\n"


def _narrowing(event: HubEvent) -> dict[str, Any]:
    """The narrowing ids this event's payload actually holds.

    A key whose value is not a string is dropped rather than raised on: the
    payload is read back from a JSON column written by many writers, and a
    malformed one must cost that frame its narrowing, never the connection.
    """
    found: dict[str, Any] = {}
    for key in NARROWING_KEYS:
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            found[key] = value
    for flag in NARROWING_FLAGS:
        if event.payload.get(flag) is True:
            found[flag] = True
    return found


def reset_frame(reason: str) -> str:
    """Tells the client to refetch everything it shows. Carries no ``id:`` so
    the client's cursor is untouched."""
    return f"event: reset\ndata: {SseResetData(reason=reason).model_dump_json()}\n\n"


def error_frame(code: str) -> str:
    """The last frame before the server closes a stream the client may not
    keep (its session ended). The client reconnects, is refused with 401, and
    hands the user to the login screen."""
    return f"event: error\ndata: {json.dumps({'code': code})}\n\n"


def parse_cursor(value: str | None) -> int | None:
    """The outbox id a client wants to resume after, or ``None`` when it sent
    nothing usable. ``Last-Event-ID`` is whatever the browser last saw, always
    one of our own ids, so anything else — text, a negative number, a value
    wider than a bigint — is treated as no cursor rather than an error."""
    if value is None:
        return None
    text = value.strip()
    # ``isdigit`` alone admits every Unicode digit; a cursor is ASCII decimal.
    if not text or not text.isascii() or not text.isdigit():
        return None
    cursor = int(text)
    if cursor >= 2**63:
        return None
    return cursor


__all__ = [
    "CONNECTED_COMMENT",
    "ERROR_UNAUTHORIZED",
    "FRAMED_ID_MEMORY",
    "KEEPALIVE_COMMENT",
    "RESET_CURSOR_AHEAD",
    "RESET_CURSOR_TOO_OLD",
    "RESET_OVERFLOW",
    "RETRY_AFTER_DEADLINE_MS",
    "RETRY_MS",
    "FramedIds",
    "cursor_frame",
    "error_frame",
    "event_frame",
    "parse_cursor",
    "reset_frame",
    "retry_frame",
]
