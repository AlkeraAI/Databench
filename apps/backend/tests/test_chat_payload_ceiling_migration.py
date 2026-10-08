"""0139's downgrade, driven against real Postgres with an oversized event already written.

The upgrade raises two payload CHECKs from 16 KiB to 2 MiB. The downgrade has to
put them back on a log that cannot be rewritten: ``event_outbox`` is append-only
-- migration 0097 puts a ``BEFORE UPDATE OR DELETE ... FOR EACH ROW`` trigger on
it -- so the obvious narrowing, delete what no longer fits and re-add the
validated CHECK, aborts the whole downgrade the moment the log carries one event
the 16 KiB bound would have refused.

That abort needs a row to fire on. A DELETE matching nothing fires no row
trigger and the narrowing then validates cleanly, so on a log with no oversized
event the broken shape and the correct one are indistinguishable -- which is why
this case writes the event first and makes the difference deterministic.

What the revision decided is that the log keeps what it already carries and only
new events are bounded again, so both halves are asserted here: at 0138 the
oversized event is still there byte for byte, and a *fresh* oversized insert is
still refused -- the half a ``NOT VALID`` constraint could silently lose.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.asyncio

_PARENT = "0140"
#: What the CHECK holds at 0140, repeated from the revision's own ``PRE_IMAGE_BYTES``.
_PRE_IMAGE_BYTES = 16 * 1024
_CONSTRAINT = "ck_event_outbox_payload_size"

_INSERT = (
    "INSERT INTO event_outbox (event_id, org_id, type, entity, entity_id, visibility, payload) "
    "VALUES (:event_id, :org_id, 'doc.op', 'chat', :entity_id, 'org', CAST(:payload AS jsonb))"
)


async def _insert(session: AsyncSession, event_id: uuid.UUID, payload: str) -> None:
    await session.execute(
        text(_INSERT),
        {
            "event_id": event_id,
            "org_id": uuid.uuid4(),
            "entity_id": str(event_id),
            "payload": payload,
        },
    )


async def _payload_text(session: AsyncSession, event_id: uuid.UUID) -> str | None:
    """The stored payload exactly as Postgres renders it, or ``None`` if it is gone."""
    return (
        await session.execute(
            text("SELECT payload::text FROM event_outbox WHERE event_id = :event_id"),
            {"event_id": event_id},
        )
    ).scalar_one_or_none()


async def _validated(session: AsyncSession) -> bool:
    """Whether the payload CHECK is one Postgres has proved against every row."""
    return bool(
        (
            await session.execute(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname = :name AND conrelid = 'event_outbox'::regclass"
                ),
                {"name": _CONSTRAINT},
            )
        ).scalar_one()
    )


async def test_the_downgrade_keeps_the_events_already_written_and_still_bounds_new_ones() -> None:
    """An oversized event survives the trip to 0138; a new one is still refused there."""
    event_id = uuid.uuid4()
    payload = json.dumps({"text": "x" * 20_000})
    assert len(payload.encode()) > _PRE_IMAGE_BYTES, "the fixture has to exceed the old bound"

    async with migration_scratch() as db:
        async with db.session() as session:
            await _insert(session, event_id, payload)
            await session.commit()
            assert await _validated(session) is True, "head's 2 MiB CHECK is a validated one"
            written = await _payload_text(session, event_id)
        assert written is not None
        assert len(written.encode()) > _PRE_IMAGE_BYTES

        # Reaching this body at all is the first assertion: a downgrade that tried to
        # delete the event above would have been refused by the append-only trigger
        # and raised out of here instead.
        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _payload_text(session, event_id) == written, (
                "the log lost an event it had already written, or rewrote one"
            )
            assert await _validated(session) is False, (
                "the old bound came back validated, which it can only do by "
                "rewriting a log that is not allowed to be rewritten"
            )

        async with db.session() as session:
            with pytest.raises(IntegrityError, match=_CONSTRAINT):
                await _insert(session, uuid.uuid4(), payload)
                await session.commit()
            await session.rollback()

        async with db.session() as session:
            small = json.dumps({"text": "x"})
            await _insert(session, uuid.uuid4(), small)
            await session.commit()

        await db.upgrade()
        async with db.session() as session:
            assert await _payload_text(session, event_id) == written
            assert await _validated(session) is True, (
                "the trip back to head re-proves the 2 MiB bound"
            )
