"""The outbox against a real Postgres: transactional visibility, the append-only
and notify triggers, the cursor reads, and the org-root walk.

The NOTIFY assertions use a raw asyncpg connection with ``LISTEN`` so they prove
what Postgres delivers, not what a Python layer relays.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from alkera_core.authz import ActingContext, ActorChainRecord
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import (
    MAX_PAYLOAD_BYTES,
    VISIBILITY_PLATFORM,
    Entity,
    EventType,
    actor_for_user,
    actor_system,
    asyncpg_dsn,
    emit,
    latest_id,
    org_root_for_team,
    read_after,
    read_ids,
)
from alkera_core.events.outbox import DEFAULT_ACTOR_LABEL, payload_size, unstorable_number
from alkera_core.models import EventOutbox, Team, User
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_NOTIFY_CHANNEL = "alkera_events"
_NOTIFY_TIMEOUT = 5.0


class _RawListener:
    """A second Postgres session that only LISTENs; what it receives is what
    Postgres actually delivered."""

    def __init__(self) -> None:
        self.received: asyncio.Queue[str] = asyncio.Queue()
        self._conn: asyncpg.Connection | None = None

    async def __aenter__(self) -> _RawListener:
        self._conn = await asyncpg.connect(asyncpg_dsn(settings.database_url))
        await self._conn.add_listener(_NOTIFY_CHANNEL, self._on_notify)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._conn is not None
        await self._conn.close()

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        self.received.put_nowait(payload)

    async def wait_for(self, payload: str, within: float = _NOTIFY_TIMEOUT) -> list[str]:
        """Every payload received up to and including ``payload``; fails past
        ``within`` seconds (a notification that never comes is the bug under test)."""
        seen: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                pytest.fail(f"no NOTIFY {payload!r} within {within}s; got {seen!r}")
            item = await asyncio.wait_for(self.received.get(), timeout=remaining)
            seen.append(item)
            if item == payload:
                return seen

    async def nothing_for(self, seconds: float) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self.received.get(), timeout=seconds)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def other_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as s:
        yield s


def _payload_of(size: int) -> dict[str, str]:
    """A payload whose JSON text is exactly ``size`` bytes: ``{"k": "aaa…"}``
    is nine bytes of framing plus the string."""
    body = {"k": "a" * (size - 9)}
    assert len(json.dumps(body).encode()) == size
    return body


@pytest.mark.parametrize(
    ("payload", "size"),
    [
        pytest.param({}, 2, id="empty"),
        pytest.param({"k": "v"}, 10, id="spaced-like-jsonb-text"),
        pytest.param(_payload_of(MAX_PAYLOAD_BYTES), MAX_PAYLOAD_BYTES, id="exactly-the-cap"),
        pytest.param({"k": "é"}, 15, id="non-ascii-measured-escaped"),
        # Postgres prints a JSON number positionally, however Python wrote it:
        # ``1e-300`` is six bytes here and three hundred and two in the table.
        pytest.param({"k": 1e-300}, 309, id="tiny-float-measured-as-stored"),
        pytest.param({"k": 1e300}, 308, id="huge-float-measured-as-stored"),
        pytest.param({"k": 1.5e-5}, 15, id="small-float-measured-as-stored"),
        pytest.param({"k": [1e-300, 1e-300]}, 615, id="floats-nested-in-a-list"),
        pytest.param({"k": 100.0}, 12, id="float-postgres-prints-shorter"),
    ],
)
def test_payload_size_is_the_measure_emit_applies(payload: dict[str, Any], size: int) -> None:
    """A producer sizing a payload before it builds the row must see the number
    ``emit`` will judge — the same spacing and escaping — so a payload it
    accepts is never refused on the emit and never measures below the stored
    text."""
    assert payload_size(payload) == size
    assert payload_size(payload) >= len(json.dumps(payload, ensure_ascii=False).encode())


async def _reload(row_id: int) -> EventOutbox:
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(EventOutbox).where(EventOutbox.id == row_id))).scalar_one()


# ---------------------------------------------------------------------------
# emit: transactional, validated, defaulted
# ---------------------------------------------------------------------------


async def test_emit_flushes_into_the_callers_transaction_and_lands_only_at_commit(
    session: AsyncSession, other_session: AsyncSession
) -> None:
    row = await emit(
        session,
        org_id=uuid4(),
        type=EventType.KB_ITEM_CHANGED,
        entity=Entity.KB_ITEM,
        entity_id="item-1",
    )
    assert row.id is not None and row.id > 0
    # Visible to the emitter's own transaction…
    assert await read_ids(session, [row.id]) == [row]
    # …and to nobody else until it commits.
    assert await read_ids(other_session, [row.id]) == []
    await session.commit()
    assert [r.id for r in await read_ids(other_session, [row.id])] == [row.id]


async def test_emit_defaults_version_visibility_actor_and_payload(session: AsyncSession) -> None:
    org = uuid4()
    row = await emit(
        session, org_id=org, type="gate_run.ingested", entity="gate_run", entity_id="r"
    )
    await session.commit()
    loaded = await _reload(row.id)
    assert loaded.org_id == org
    assert loaded.type == "gate_run.ingested"
    assert loaded.entity == "gate_run"
    assert loaded.entity_id == "r"
    assert loaded.version == 0
    assert loaded.visibility == "org"
    assert loaded.payload == {}
    assert loaded.actor == actor_system(DEFAULT_ACTOR_LABEL)
    assert loaded.event_id is not None
    assert loaded.ts is not None and loaded.ts.tzinfo is not None


async def test_emit_stores_every_field_it_is_given(session: AsyncSession) -> None:
    org, user_id = uuid4(), uuid4()
    payload = {"team_id": str(uuid4()), "nested": {"n": 1, "list": [1, 2, 3]}, "text": "héllo"}
    row = await emit(
        session,
        org_id=org,
        type=EventType.BILLING_SUMMARY_CHANGED,
        entity=Entity.BILLING_ACCOUNT,
        entity_id=str(user_id),
        version=7,
        payload=payload,
        actor=actor_system("stripe:invoice.paid"),
        visibility=f"user:{user_id}",
    )
    await session.commit()
    loaded = await _reload(row.id)
    assert loaded.version == 7
    assert loaded.payload == payload
    assert loaded.visibility == f"user:{user_id}"
    assert loaded.actor["acting"]["id"] == "stripe:invoice.paid"


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        pytest.param({"type": "kb_item.deleted"}, ValueError, "unknown event type", id="type"),
        pytest.param({"entity": ""}, ValueError, "entity must be", id="entity-empty"),
        pytest.param({"entity": "e" * 65}, ValueError, "entity must be", id="entity-too-long"),
        pytest.param({"entity_id": ""}, ValueError, "entity_id must be", id="entity-id-empty"),
        pytest.param(
            {"entity_id": "x" * 256}, ValueError, "entity_id must be", id="entity-id-too-long"
        ),
        pytest.param({"version": -1}, ValueError, "version must be >= 0", id="version-negative"),
        pytest.param({"visibility": "team:x"}, ValueError, "invalid visibility", id="visibility"),
        pytest.param(
            {"visibility": f"USER:{uuid4()}"},
            ValueError,
            "invalid visibility",
            id="visibility-case",
        ),
        pytest.param(
            {"payload": _payload_of(MAX_PAYLOAD_BYTES + 1)},
            ValueError,
            "caps it at",
            id="payload-one-over",
        ),
        pytest.param(
            {"payload": {"id": uuid4()}}, TypeError, "not JSON serializable", id="payload-not-json"
        ),
        pytest.param({"actor": {"acting": {}}}, ValueError, "acting", id="actor-malformed"),
        pytest.param({"actor": {"kind": "user", "id": "x"}}, ValueError, "acting", id="actor-flat"),
    ],
)
async def test_emit_refuses_an_invalid_event_and_adds_nothing(
    session: AsyncSession, kwargs: dict[str, Any], error: type[Exception], match: str
) -> None:
    base: dict[str, Any] = {
        "org_id": uuid4(),
        "type": EventType.KB_ITEM_CHANGED,
        "entity": Entity.KB_ITEM,
        "entity_id": "item",
    }
    base.update(kwargs)
    with pytest.raises(error, match=match):
        await emit(session, **base)
    assert not session.new and not session.dirty


async def test_a_payload_at_the_cap_is_accepted_by_emit_and_by_the_table(
    session: AsyncSession,
) -> None:
    """The Python measure and the table's CHECK agree at the boundary, so a
    payload emit accepts is never refused at flush."""
    row = await emit(
        session,
        org_id=uuid4(),
        type=EventType.KB_ITEM_CHANGED,
        entity=Entity.KB_ITEM,
        entity_id="big",
        payload=_payload_of(MAX_PAYLOAD_BYTES),
    )
    await session.commit()
    assert len(json.dumps((await _reload(row.id)).payload).encode()) == MAX_PAYLOAD_BYTES


async def _stored_octets(row_id: int) -> int:
    """What the table's CHECK measures: ``octet_length(payload::text)``, read
    back from Postgres itself."""
    async with AsyncSessionLocal() as s:
        return int(
            (
                await s.execute(
                    text("SELECT octet_length(payload::text) FROM event_outbox WHERE id = :id"),
                    {"id": row_id},
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"cost": 1e-300}, id="tiny-float"),
        pytest.param({"cost": 1e300}, id="huge-float"),
        pytest.param({"cost": 1.5e-5}, id="small-float"),
        pytest.param({"cost": 0.1, "count": 3, "ratio": 100.0}, id="ordinary-numbers"),
        pytest.param({"rows": [1e-300, 2.5e-8, 7]}, id="numbers-in-a-list"),
        pytest.param({"deep": {"a": {"b": 6.02e-24}}}, id="numbers-nested"),
        pytest.param({"note": "é" * 20, "n": 1e-30}, id="non-ascii-and-a-float"),
        pytest.param({"big": 10**60}, id="a-huge-integer"),
    ],
)
async def test_the_measure_is_never_below_what_postgres_stores(
    session: AsyncSession, payload: dict[str, Any]
) -> None:
    """The whole point of :func:`payload_size`: a producer that sized a payload
    under the cap must never have the table's CHECK refuse it. Postgres prints
    a JSON number positionally whatever notation it was written in, so the
    measure is checked against what Postgres actually stored, not against
    Python's own rendering."""
    row = await emit(
        session,
        org_id=uuid4(),
        type=EventType.KB_ITEM_CHANGED,
        entity=Entity.KB_ITEM,
        entity_id="measured",
        payload=payload,
    )
    await session.commit()
    assert payload_size(payload) >= await _stored_octets(row.id)


async def test_a_payload_of_tiny_floats_is_refused_by_emit_and_by_the_table(
    session: AsyncSession, other_session: AsyncSession
) -> None:
    """A payload whose Python text is half a kilobyte and whose stored text is
    eighteen. Both halves of the bound are pinned: ``emit`` refuses it, and the
    table would have refused it too — the row cannot be smuggled past the
    friendly check into a CHECK violation that kills the request."""
    # 1e-300 is six bytes of Python text and three hundred and two in the table,
    # so the count is derived from the cap rather than spelled: the shape of the
    # test survives the cap moving, which a literal would not.
    payload = {"rows": [1e-300] * (MAX_PAYLOAD_BYTES // 296 + 64)}
    assert len(json.dumps(payload).encode()) < MAX_PAYLOAD_BYTES, "Python renders it small"
    assert payload_size(payload) > MAX_PAYLOAD_BYTES, "Postgres will not"

    with pytest.raises(ValueError, match="caps it at"):
        await emit(
            session,
            org_id=uuid4(),
            type=EventType.KB_ITEM_CHANGED,
            entity=Entity.KB_ITEM,
            entity_id="tiny-floats",
            payload=payload,
        )
    assert not session.new and not session.dirty

    other_session.add(
        EventOutbox(
            event_id=uuid4(),
            org_id=uuid4(),
            type=EventType.KB_ITEM_CHANGED.value,
            entity=Entity.KB_ITEM.value,
            entity_id="tiny-floats",
            version=0,
            actor=actor_system("test"),
            visibility="org",
            payload=payload,
        )
    )
    with pytest.raises(IntegrityError, match="ck_event_outbox_payload_size"):
        await other_session.flush()
    await other_session.rollback()


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
async def test_emit_refuses_a_number_jsonb_cannot_store(
    session: AsyncSession, other_session: AsyncSession, value: float
) -> None:
    """``NaN`` and the infinities are not JSON, but Python's reader accepts
    their literals and its writer emits them again, so one can travel from a
    client frame to the INSERT. There it is not a CHECK violation but a parse
    error that takes the whole transaction down, so ``emit`` refuses it as a
    bad payload instead."""
    payload = {"ts": value}
    assert unstorable_number(payload) is not None
    with pytest.raises(ValueError, match="jsonb cannot store"):
        await emit(
            session,
            org_id=uuid4(),
            type=EventType.KB_ITEM_CHANGED,
            entity=Entity.KB_ITEM,
            entity_id="not-a-number",
            payload=payload,
        )
    assert not session.new and not session.dirty

    other_session.add(
        EventOutbox(
            event_id=uuid4(),
            org_id=uuid4(),
            type=EventType.KB_ITEM_CHANGED.value,
            entity=Entity.KB_ITEM.value,
            entity_id="not-a-number",
            version=0,
            actor=actor_system("test"),
            visibility="org",
            payload=payload,
        )
    )
    with pytest.raises(DBAPIError):
        await other_session.flush()
    await other_session.rollback()


def test_only_a_number_postgres_cannot_store_is_reported() -> None:
    """The question is about the numbers, and only the impossible ones."""
    assert unstorable_number({"a": 1.0, "b": [2, 3], "c": {"d": "nan"}}) is None
    assert unstorable_number({}) is None
    assert unstorable_number({"a": [{"b": float("inf")}]}) == float("inf")


async def test_emit_normalises_the_actor_document(session: AsyncSession) -> None:
    """An actor handed in without its version stamp is stored as a full
    ``ActorChainRecord`` dump, so every row's actor loads through the same
    versioned reader."""
    bare = {"acting": {"kind": "service", "id": "worker", "org_id": ""}}
    row = await emit(
        session, org_id=uuid4(), type="doc.op", entity="doc", entity_id="d", actor=bare
    )
    await session.commit()
    stored = (await _reload(row.id)).actor
    assert stored["schema_version"] == ActorChainRecord.SCHEMA_VERSION
    assert stored["acting"]["schema_version"] == ActorChainRecord.SCHEMA_VERSION
    assert stored["acting"]["label"] == ""
    assert stored["chain"] == []


async def test_an_actor_document_round_trips_through_the_row(session: AsyncSession) -> None:
    user = User(
        id=uuid4(), home_org_team_id=uuid4(), email="who@example.com", first_name="W", last_name="H"
    )
    document = actor_for_user(user, org_id=user.home_org_team_id)
    row = await emit(
        session,
        org_id=user.home_org_team_id,
        type=EventType.MEMBERSHIP_CHANGED,
        entity=Entity.MEMBERSHIP,
        entity_id=str(user.id),
        actor=document,
    )
    await session.commit()
    stored = (await _reload(row.id)).actor
    assert stored == document
    record = ActorChainRecord.model_validate(stored)
    assert record.acting.id == str(user.id)
    assert record.acting.org_id == str(user.home_org_team_id)
    assert record.acting.label == ""


async def test_an_acting_context_audit_dict_is_persisted_unchanged(session: AsyncSession) -> None:
    """The authorization layer passes ``ctx.audit_dict()`` as the actor of its
    decision rows; the outbox must store it byte-for-byte."""
    ctx = ActingContext.for_agent(
        user_id=uuid4(), org_id=uuid4(), email="owner@example.com", session_id="sess-1"
    )
    document = ctx.audit_dict()
    row = await emit(
        session,
        org_id=ctx.org_id,
        type="authz.decision",
        entity="kb_item",
        entity_id="k",
        visibility="platform",
        actor=document,
    )
    await session.commit()
    assert (await _reload(row.id)).actor == document


# ---------------------------------------------------------------------------
# append-only
# ---------------------------------------------------------------------------


async def _committed_row() -> EventOutbox:
    async with AsyncSessionLocal() as s:
        row = await emit(
            s, org_id=uuid4(), type=EventType.CHAT_UPDATED, entity=Entity.CHAT, entity_id="c"
        )
        await s.commit()
        return row


async def test_update_is_refused_by_the_append_only_trigger() -> None:
    row = await _committed_row()
    async with AsyncSessionLocal() as s:
        with pytest.raises(IntegrityError, match="append-only: UPDATE refused"):
            await s.execute(update(EventOutbox).where(EventOutbox.id == row.id).values(version=99))
        await s.rollback()
    assert (await _reload(row.id)).version == 0


async def test_delete_is_refused_by_the_append_only_trigger() -> None:
    row = await _committed_row()
    async with AsyncSessionLocal() as s:
        with pytest.raises(IntegrityError, match="append-only: DELETE refused"):
            await s.execute(delete(EventOutbox).where(EventOutbox.id == row.id))
        await s.rollback()
    assert (await _reload(row.id)).id == row.id


async def test_orm_mutation_of_a_loaded_row_is_refused_too() -> None:
    """The trigger guards the ORM path as well: a loaded row edited in place
    fails at flush, and the row is untouched."""
    row = await _committed_row()
    async with AsyncSessionLocal() as s:
        loaded = (await s.execute(select(EventOutbox).where(EventOutbox.id == row.id))).scalar_one()
        loaded.entity_id = "tampered"
        with pytest.raises(IntegrityError, match="append-only"):
            await s.flush()
        await s.rollback()
    assert (await _reload(row.id)).entity_id == "c"


# ---------------------------------------------------------------------------
# cursor reads
# ---------------------------------------------------------------------------


async def test_read_after_pages_in_id_order_and_filters_by_org(session: AsyncSession) -> None:
    org_a, org_b = uuid4(), uuid4()
    start = await latest_id(session)
    ids: list[int] = []
    for org, entity_id in ((org_a, "a1"), (org_b, "b1"), (org_a, "a2"), (org_a, "a3")):
        row = await emit(
            session,
            org_id=org,
            type=EventType.KB_ITEM_CHANGED,
            entity="kb_item",
            entity_id=entity_id,
        )
        ids.append(row.id)
    await session.commit()

    everything = await read_after(session, after_id=start)
    assert [r.id for r in everything] == ids
    assert ids == sorted(ids)

    only_a = await read_after(session, after_id=start, org_id=org_a)
    assert [r.entity_id for r in only_a] == ["a1", "a2", "a3"]

    after_first_a = await read_after(session, after_id=ids[0], org_id=org_a)
    assert [r.entity_id for r in after_first_a] == ["a2", "a3"]

    page = await read_after(session, after_id=start, limit=2)
    assert [r.id for r in page] == ids[:2]

    assert await read_after(session, after_id=ids[-1]) == []
    assert await latest_id(session) >= ids[-1]


async def test_read_after_refuses_a_zero_or_negative_limit(session: AsyncSession) -> None:
    for limit in (0, -5):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await read_after(session, after_id=0, limit=limit)


async def test_read_ids_returns_the_named_rows_in_id_order_and_skips_missing(
    session: AsyncSession,
) -> None:
    rows = [
        await emit(
            session, org_id=uuid4(), type="kb_item.changed", entity="kb_item", entity_id=str(i)
        )
        for i in range(3)
    ]
    await session.commit()
    ids = [r.id for r in rows]
    found = await read_ids(session, [ids[2], ids[0], 10**12, ids[1]])
    assert [r.id for r in found] == ids
    assert await read_ids(session, []) == []


async def test_latest_id_is_the_highest_committed_id(session: AsyncSession) -> None:
    before = await latest_id(session)
    row = await emit(
        session, org_id=uuid4(), type="kb_item.changed", entity="kb_item", entity_id="x"
    )
    await session.commit()
    assert await latest_id(session) == row.id >= before + 1


# ---------------------------------------------------------------------------
# NOTIFY at commit
# ---------------------------------------------------------------------------


async def test_notify_reaches_a_raw_listener_with_the_row_id_only_at_commit(
    session: AsyncSession,
) -> None:
    async with _RawListener() as listener:
        row = await emit(
            session, org_id=uuid4(), type=EventType.KB_ITEM_CHANGED, entity="kb_item", entity_id="n"
        )
        # Flushed, not committed: Postgres holds the notification.
        await listener.nothing_for(0.5)
        await session.commit()
        received = await listener.wait_for(str(row.id))
        assert received[-1] == str(row.id)


async def test_a_rolled_back_emit_never_notifies(session: AsyncSession) -> None:
    async with _RawListener() as listener:
        ghost = await emit(
            session, org_id=uuid4(), type=EventType.KB_ITEM_CHANGED, entity="kb_item", entity_id="g"
        )
        await session.rollback()
        await listener.nothing_for(0.5)
        # A later committed emit still arrives — the listener is live — and the
        # rolled-back id is never among what was delivered.
        real = await emit(
            session, org_id=uuid4(), type=EventType.KB_ITEM_CHANGED, entity="kb_item", entity_id="r"
        )
        await session.commit()
        received = await listener.wait_for(str(real.id))
        assert str(ghost.id) not in received
        assert await read_ids(session, [ghost.id]) == []


async def test_a_platform_row_commits_without_ringing_the_doorbell(session: AsyncSession) -> None:
    """Every authorized request writes a platform-only ``authz.decision`` row. A
    NOTIFY puts its commit in the database-wide queue Postgres serializes
    notifying commits through, so a read that rang one waited behind any slow
    flush and answered 503 past ``lock_timeout``. The row still lands for the
    cursor; only rows a tenant stream receives ring."""
    async with _RawListener() as listener:
        quiet = await emit(
            session,
            org_id=uuid4(),
            type=EventType.KB_ITEM_CHANGED,
            entity="kb_item",
            entity_id="p",
            visibility=VISIBILITY_PLATFORM,
        )
        await session.commit()
        await listener.nothing_for(0.5)
        loud = await emit(
            session, org_id=uuid4(), type=EventType.KB_ITEM_CHANGED, entity="kb_item", entity_id="o"
        )
        await session.commit()
        received = await listener.wait_for(str(loud.id))
        assert str(quiet.id) not in received
        assert [row.id for row in await read_ids(session, [quiet.id])] == [quiet.id]


async def test_every_row_of_one_transaction_notifies_once(session: AsyncSession) -> None:
    async with _RawListener() as listener:
        rows = [
            await emit(
                session, org_id=uuid4(), type="kb_item.changed", entity="kb_item", entity_id=str(i)
            )
            for i in range(3)
        ]
        await session.commit()
        received = await listener.wait_for(str(rows[-1].id))
        assert received == [str(r.id) for r in rows]


# ---------------------------------------------------------------------------
# org root
# ---------------------------------------------------------------------------


async def _team(session: AsyncSession, *, parent: UUID | None, is_root: bool = False) -> UUID:
    team = Team(name=f"t-{secrets.token_hex(3)}", parent_team_id=parent, is_root=is_root)
    session.add(team)
    await session.flush()
    return team.id


async def test_org_root_for_team_walks_to_the_root_and_tolerates_a_bad_id(
    session: AsyncSession,
) -> None:
    root = await _team(session, parent=None, is_root=True)
    child = await _team(session, parent=root)
    grandchild = await _team(session, parent=child)
    await session.commit()
    assert await org_root_for_team(session, grandchild) == root
    assert await org_root_for_team(session, child) == root
    assert await org_root_for_team(session, root) == root
    unknown = uuid4()
    assert await org_root_for_team(session, unknown) == unknown


async def test_org_root_for_team_terminates_on_a_cycle(session: AsyncSession) -> None:
    """A malformed tree (a ``parent_team_id`` cycle) must yield an answer, not
    an unbounded walk inside the event loop."""
    a = await _team(session, parent=None)
    b = await _team(session, parent=a)
    await session.execute(update(Team).where(Team.id == a).values(parent_team_id=b))
    await session.commit()
    try:
        assert await asyncio.wait_for(org_root_for_team(session, b), timeout=5.0) == a
        assert await asyncio.wait_for(org_root_for_team(session, a), timeout=5.0) == b
    finally:
        await session.execute(update(Team).where(Team.id == a).values(parent_team_id=None))
        await session.commit()


@pytest.mark.parametrize(
    ("configured", "size", "refused"),
    [
        pytest.param(65536, 65537, True, id="one-over-a-tightened-ceiling"),
        pytest.param(65536, 65536, False, id="exactly-a-tightened-ceiling"),
        pytest.param(65536, 200_000, True, id="well-under-the-absolute-but-over-the-setting"),
        pytest.param(MAX_PAYLOAD_BYTES, 200_000, False, id="default-ceiling-admits-it"),
    ],
)
async def test_the_payload_ceiling_a_deployment_configures_is_the_one_emit_applies(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    configured: int,
    size: int,
    refused: bool,
) -> None:
    """A chat message reaches the other people in the chat as one of these rows,
    so this ceiling is what a long paste is ultimately refused by, and it is an
    operator's to tighten: a deployment on a small Postgres brings it down
    without a release, and the number in the refusal is the one in force."""
    monkeypatch.setattr(settings, "event_outbox_payload_max_bytes", configured)
    kwargs: dict[str, Any] = {
        "org_id": uuid4(),
        "type": EventType.KB_ITEM_CHANGED,
        "entity": "kb_item",
        "entity_id": "i",
        "payload": _payload_of(size),
    }
    if not refused:
        row = await emit(session, **kwargs)
        assert row.id is not None
        return
    with pytest.raises(ValueError, match=f"caps it at {configured}"):
        await emit(session, **kwargs)
    assert not session.new, "a refused emit adds nothing to the transaction"
