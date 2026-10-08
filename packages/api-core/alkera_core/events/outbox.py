"""Append rows to the event outbox, inside the caller's transaction.

The outbox is the one place a mutation announces itself. :func:`emit` adds a
row to the CALLER's session and flushes it — it never commits. The row becomes
visible, and its ``AFTER INSERT`` trigger's ``pg_notify`` fires, exactly when
the caller's transaction commits; a rollback leaves no row and sends nothing.
That is what makes the announcement trustworthy: a consumer never learns about
a change that did not happen.

NOTIFY is a doorbell only. Its payload is the row id; the table is the cursor
a consumer reads from, so a notification lost across a reconnect costs nothing
but latency. The payload therefore stays in the table, bounded by
:data:`MAX_PAYLOAD_BYTES` so one producer cannot bloat the log; the frame a
client receives is thin.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.authz.actor_chain import ActorChainRecord
from alkera_core.config import get_settings
from alkera_core.doc_type_names import respell_channel, respell_payload
from alkera_core.events.actor import actor_system
from alkera_core.events.types import (
    VISIBILITY_ORG,
    Entity,
    EventType,
    coerce_event_type,
    validate_visibility,
)
from alkera_core.models.event_outbox import EventOutbox

#: Upper bound on the JSON text of a payload. The table repeats it as a CHECK
#: (``ck_event_outbox_payload_size``); the check here is the friendly one.
#:
#: A chat message travels to the other people in the chat as one of these rows,
#: so this is the bound a person's typed message is ultimately refused by, and
#: it is sized for the longest one anybody may send rather than for the small
#: state-change notices that are the rest of the traffic. It must stay UNDER
#: ``settings.realtime_doc_max_bytes``: a document op is refused here first, and
#: the state it then lands in has to be able to hold it.
#: The ABSOLUTE ceiling, which the table's CHECK repeats. The operational one
#: is ``settings.event_outbox_payload_max_bytes``, read per emit, which
#: defaults to this and which a deployment may set lower — never higher,
#: because a row past the CHECK would be refused by Postgres instead.
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
#: Headroom for the websocket envelope a payload travels inside: the frame's
#: type tag, channel, sequence and the JSON escaping around the payload itself.
_FRAME_ENVELOPE_BYTES = 64 * 1024
#: Upper bound on one websocket frame, on BOTH ends of the socket — the
#: gateway refuses a larger one with ``frame_too_large`` and the box refuses to
#: send one. It is derived from the payload cap rather than written down
#: separately so a row the event log accepts always fits a single frame; when
#: the two were independent literals, every payload between them put the box in
#: a reconnect loop and the chat looked stuck.
MAX_FRAME_BYTES = MAX_PAYLOAD_BYTES + _FRAME_ENVELOPE_BYTES
MAX_ENTITY_LENGTH = 64
MAX_ENTITY_ID_LENGTH = 255
#: The actor a producer that names none is recorded as.
DEFAULT_ACTOR_LABEL = "backend"


def _numbers(value: Any) -> Iterator[float]:
    """Every float anywhere in ``value``. Integers and booleans render the same
    in both places and are not interesting here."""
    if isinstance(value, float):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _numbers(item)


def _postgres_widening(payload: Mapping[str, Any]) -> int:
    """How many bytes wider than ``json.dumps`` Postgres renders the numbers in
    ``payload``.

    Postgres parses a JSON number into ``numeric`` and prints it positionally,
    so ``1e-300`` is six bytes of Python text and three hundred and two bytes
    in the table. Nothing else in a JSON document renders wider there than
    here, so this is the whole correction. A number Postgres renders SHORTER
    (``100.0`` stores as ``100``) contributes nothing: the measure may sit
    above the stored text, never below it.
    """
    extra = 0
    for number in _numbers(payload):
        if not math.isfinite(number):
            # Not storable at all (see `unstorable_number`); nothing to measure.
            continue
        rendered = repr(number)
        if "e" in rendered or "E" in rendered:
            extra += max(0, len(format(Decimal(rendered), "f")) - len(rendered))
    return extra


def unstorable_number(payload: Mapping[str, Any]) -> float | None:
    """The first number in ``payload`` that Postgres cannot store, or ``None``.

    ``NaN`` and the infinities are not JSON, but Python's JSON reader accepts
    their literals and its writer emits them again, so one can travel from a
    client's frame all the way to the INSERT — where ``jsonb`` refuses the
    document and takes the whole transaction down. A producer asks this the
    way it asks :func:`payload_size`, and refuses in its own vocabulary.
    """
    return next((n for n in _numbers(payload) if not math.isfinite(n)), None)


def payload_size(payload: Mapping[str, Any]) -> int:
    """The bytes ``emit`` measures ``payload`` at, against :data:`MAX_PAYLOAD_BYTES`.

    Default separators render the same spacing Postgres uses for
    ``jsonb::text``; the default ``ensure_ascii`` escapes non-ASCII to six
    bytes where Postgres stores two to four; and numbers are corrected for the
    positional form Postgres prints them in (see :func:`_postgres_widening`).
    So this measure is never smaller than the stored text: a payload accepted
    here is never refused by the table's CHECK. Public so a producer can size
    a payload it is about to build and refuse in its own vocabulary instead of
    failing on the emit.

    A payload carrying a number Postgres cannot store at all is not measurable
    against the CHECK; :func:`unstorable_number` is the question for that one.
    """
    body = dict(payload)
    return len(json.dumps(body).encode("utf-8")) + _postgres_widening(body)


async def emit(
    session: AsyncSession,
    *,
    org_id: UUID,
    type: EventType | str,
    entity: Entity | str,
    entity_id: str,
    version: int = 0,
    payload: Mapping[str, Any] | None = None,
    actor: Mapping[str, Any] | None = None,
    visibility: str = VISIBILITY_ORG,
    flush: bool = True,
) -> EventOutbox:
    """Add one event to the caller's transaction and flush it.

    ``flush=False`` adds the row without flushing, for a producer announcing
    several events at once: its own ``session.flush()`` then writes them in one
    multi-row statement rather than one each. The row's ``id`` is unset until
    that flush.

    Returns the row with its ``id`` populated. Raises ``ValueError`` on an
    unknown ``type``, an empty or over-long ``entity`` / ``entity_id``, a
    negative ``version``, a payload over :data:`MAX_PAYLOAD_BYTES` or carrying
    a number ``jsonb`` cannot store, a ``visibility`` outside the grammar, or
    an ``actor`` that is not an ``ActorChainRecord`` document; ``TypeError``
    when the payload is not JSON.
    Nothing is added to the session unless every check passes. Never commits.
    """
    event_type = coerce_event_type(type)
    entity_name = str(entity)
    if not entity_name or len(entity_name) > MAX_ENTITY_LENGTH:
        raise ValueError(f"entity must be 1..{MAX_ENTITY_LENGTH} characters, got {entity_name!r}")
    if not entity_id or len(entity_id) > MAX_ENTITY_ID_LENGTH:
        raise ValueError(f"entity_id must be 1..{MAX_ENTITY_ID_LENGTH} characters")
    if version < 0:
        raise ValueError(f"version must be >= 0, got {version}")
    validate_visibility(visibility)
    # Spelled the way the previous build reads it, so a replica still on it
    # during a roll understands the row; every reader respells it back.
    body: dict[str, Any] = respell_payload(payload or {}, "to_replicas")
    entity_id = respell_channel(entity_id, "to_replicas") or entity_id
    unstorable = unstorable_number(body)
    if unstorable is not None:
        raise ValueError(f"payload carries {unstorable!r}, which jsonb cannot store")
    size = payload_size(body)
    cap = min(get_settings().event_outbox_payload_max_bytes, MAX_PAYLOAD_BYTES)
    if size > cap:
        raise ValueError(f"payload is {size} bytes; the outbox caps it at {cap}")
    actor_document = (
        ActorChainRecord.model_validate(dict(actor)).model_dump(mode="json")
        if actor is not None
        else actor_system(DEFAULT_ACTOR_LABEL)
    )
    row = EventOutbox(
        event_id=uuid4(),
        org_id=org_id,
        type=event_type.value,
        entity=entity_name,
        entity_id=entity_id,
        version=version,
        actor=actor_document,
        visibility=visibility,
        payload=body,
    )
    session.add(row)
    if flush:
        await session.flush()
    return row


async def read_after(
    session: AsyncSession,
    *,
    after_id: int,
    org_id: UUID | None = None,
    limit: int = 500,
) -> list[EventOutbox]:
    """Rows with ``id > after_id`` in id order, at most ``limit`` of them,
    optionally for one org only."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    stmt = select(EventOutbox).where(EventOutbox.id > after_id)
    if org_id is not None:
        stmt = stmt.where(EventOutbox.org_id == org_id)
    stmt = stmt.order_by(EventOutbox.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def read_ids(session: AsyncSession, ids: Sequence[int]) -> list[EventOutbox]:
    """The rows with exactly these ids, in id order; ids that do not exist are
    simply absent."""
    if not ids:
        return []
    stmt = select(EventOutbox).where(EventOutbox.id.in_(list(ids))).order_by(EventOutbox.id)
    return list((await session.execute(stmt)).scalars().all())


async def latest_id(session: AsyncSession) -> int:
    """The highest id committed so far, ``0`` when the table is empty."""
    value = (await session.execute(select(func.coalesce(func.max(EventOutbox.id), 0)))).scalar_one()
    return int(value)


__all__ = [
    "DEFAULT_ACTOR_LABEL",
    "MAX_ENTITY_ID_LENGTH",
    "MAX_ENTITY_LENGTH",
    "MAX_FRAME_BYTES",
    "MAX_PAYLOAD_BYTES",
    "emit",
    "latest_id",
    "payload_size",
    "read_after",
    "read_ids",
]
