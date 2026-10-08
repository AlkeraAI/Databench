"""The realtime tables as the migrated database actually has them.

``alembic check`` compares columns, named indexes and unique constraints, but
not CHECK constraints or triggers — so this file asks the live catalog. Every
name asserted here is one a later migration may need to drop by name.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox
from alkera_core.models.event_outbox import _MAX_PAYLOAD_BYTES
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": uuid4(),
        "org_id": uuid4(),
        "type": "kb_item.changed",
        "entity": "kb_item",
        "entity_id": "item",
    }
    base.update(overrides)
    return base


async def _insert_raw(values: dict[str, Any]) -> int:
    """A Core INSERT that bypasses the application layer: what the table itself
    enforces and defaults."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            insert(EventOutbox.__table__).values(**values).returning(EventOutbox.__table__.c.id)
        )
        row_id = result.scalar_one()
        await session.commit()
        return int(row_id)


async def test_event_outbox_round_trips_through_the_orm() -> None:
    org, user = uuid4(), uuid4()
    async with AsyncSessionLocal() as session:
        row = EventOutbox(
            org_id=org,
            type="billing.summary_changed",
            entity="billing_account",
            entity_id=str(user),
            version=3,
            actor={"acting": {"kind": "service", "id": "test", "org_id": ""}},
            visibility=f"user:{user}",
            payload={"team_id": str(org)},
        )
        session.add(row)
        await session.commit()
        row_id, event_id = row.id, row.event_id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(EventOutbox).where(EventOutbox.id == row_id))
        ).scalar_one()
        assert loaded.event_id == event_id
        assert loaded.org_id == org
        assert loaded.type == "billing.summary_changed"
        assert loaded.entity == "billing_account"
        assert loaded.entity_id == str(user)
        assert loaded.version == 3
        assert loaded.actor == {"acting": {"kind": "service", "id": "test", "org_id": ""}}
        assert loaded.visibility == f"user:{user}"
        assert loaded.payload == {"team_id": str(org)}
        assert loaded.ts.tzinfo is not None


async def test_the_table_defaults_version_visibility_actor_and_payload() -> None:
    """A writer that names only the required columns gets the documented
    defaults from the table itself, not from the ORM."""
    row_id = await _insert_raw(_row())
    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(EventOutbox).where(EventOutbox.id == row_id))
        ).scalar_one()
        assert loaded.version == 0
        assert loaded.visibility == "org"
        assert loaded.actor == {}
        assert loaded.payload == {}
        assert loaded.ts is not None


async def test_ids_are_a_monotonic_bigserial() -> None:
    first = await _insert_raw(_row())
    second = await _insert_raw(_row())
    assert second > first


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"version": -1}, "ck_event_outbox_version_nonnegative", id="version"),
        pytest.param({"visibility": "team:x"}, "ck_event_outbox_visibility", id="visibility-team"),
        pytest.param(
            {"visibility": f"USER:{uuid4()}"}, "ck_event_outbox_visibility", id="visibility-case"
        ),
        pytest.param(
            {"visibility": "user:not-a-uuid"},
            "ck_event_outbox_visibility",
            id="visibility-non-uuid",
        ),
        pytest.param({"visibility": ""}, "ck_event_outbox_visibility", id="visibility-empty"),
        pytest.param(
            {"payload": {"k": "a" * (_MAX_PAYLOAD_BYTES - 8)}},
            "ck_event_outbox_payload_size",
            id="payload-over-cap",
        ),
    ],
)
async def test_the_table_refuses_what_the_application_would(
    overrides: dict[str, Any], constraint: str
) -> None:
    """The CHECK constraints are the backstop behind ``emit``'s validation: a
    writer that bypasses the application layer is refused by name."""
    with pytest.raises(IntegrityError, match=constraint):
        await _insert_raw(_row(**overrides))


async def test_a_payload_exactly_at_the_cap_is_accepted_by_the_table() -> None:
    await _insert_raw(_row(payload={"k": "a" * (_MAX_PAYLOAD_BYTES - 9)}))


async def test_event_id_is_unique() -> None:
    event_id = uuid4()
    await _insert_raw(_row(event_id=event_id))
    with pytest.raises(IntegrityError, match="uq_event_outbox_event_id"):
        await _insert_raw(_row(event_id=event_id))


async def test_org_id_has_no_foreign_key() -> None:
    """Deliberate: the append-only trigger would refuse a cascading delete and
    make an org impossible to remove. Pinned so nobody 'fixes' it."""
    assert EventOutbox.__table__.foreign_keys == set()
    async with AsyncSessionLocal() as session:
        fks = (
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'event_outbox'::regclass AND contype = 'f'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert fks == []


async def test_the_model_names_every_constraint_and_index_the_migration_creates() -> None:
    constraint_names = {c.name for c in EventOutbox.__table__.constraints if c.name}
    assert constraint_names == {
        "uq_event_outbox_event_id",
        "ck_event_outbox_visibility",
        "ck_event_outbox_version_nonnegative",
        "ck_event_outbox_payload_size",
    }
    assert {i.name for i in EventOutbox.__table__.indexes} == {"ix_event_outbox_org_id_id"}


async def test_the_migrated_database_carries_the_named_constraints_indexes_and_triggers() -> None:
    async with AsyncSessionLocal() as session:
        constraints = set(
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'event_outbox'::regclass"
                    )
                )
            ).scalars()
        )
        indexes = set(
            (
                await session.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'event_outbox'")
                )
            ).scalars()
        )
        triggers = set(
            (
                await session.execute(
                    text(
                        "SELECT t.tgname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE c.relname = 'event_outbox' AND NOT t.tgisinternal"
                    )
                )
            ).scalars()
        )
    assert {
        "uq_event_outbox_event_id",
        "ck_event_outbox_visibility",
        "ck_event_outbox_version_nonnegative",
        "ck_event_outbox_payload_size",
    } <= constraints
    assert "ix_event_outbox_org_id_id" in indexes
    assert triggers == {"trg_event_outbox_append_only", "trg_event_outbox_notify"}


# ---------------------------------------------------------------------------
# The socket-gateway tables
# ---------------------------------------------------------------------------


async def _org_and_user() -> tuple[Any, Any]:
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as session:
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Realtime Org {uuid4().hex[:6]}",
            admin_email=f"rt-{uuid4().hex[:8]}@alkera.dev",
            admin_first_name="Real",
            admin_last_name="Time",
            admin_password="pw-1234567890",
        )
        await session.commit()
        return org.id, admin.id


async def test_ws_ticket_use_round_trips_and_is_unique_by_jti() -> None:
    from alkera_core.models import WsTicketUse

    jti = f"jti-{uuid4().hex}"
    async with AsyncSessionLocal() as session:
        session.add(WsTicketUse(jti=jti))
        await session.commit()
    async with AsyncSessionLocal() as session:
        row = await session.get(WsTicketUse, jti)
        assert row is not None and row.consumed_at.tzinfo is not None
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError, match="pk_ws_ticket_uses"):
            await session.execute(insert(WsTicketUse.__table__).values(jti=jti))


async def test_realtime_doc_round_trips_with_its_defaults() -> None:
    from alkera_core.models import RealtimeDoc

    org_id, user_id = await _org_and_user()
    doc_id = f"sess-{uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            RealtimeDoc(
                doc_type="chat",
                doc_id=doc_id,
                org_id=org_id,
                owner_user_id=user_id,
                state={"events": []},
            )
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_id, "chat", doc_id))
        assert doc is not None
        assert (doc.epoch, doc.seq) == (1, 0)
        assert doc.team_id is None
        assert doc.state == {"events": []}
        assert doc.created_at.tzinfo is not None and doc.updated_at.tzinfo is not None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"doc_type": "graph"}, "ck_realtime_docs_doc_type", id="doc-type"),
        pytest.param({"epoch": 0}, "ck_realtime_docs_epoch_positive", id="epoch-zero"),
        pytest.param({"seq": -1}, "ck_realtime_docs_seq_nonnegative", id="negative-seq"),
    ],
)
async def test_realtime_docs_refuses_what_the_application_would(
    overrides: dict[str, Any], constraint: str
) -> None:
    from alkera_core.models import RealtimeDoc

    org_id, user_id = await _org_and_user()
    values: dict[str, Any] = {
        "doc_type": "artifact",
        "doc_id": uuid4().hex,
        "org_id": org_id,
        "owner_user_id": user_id,
        "epoch": 1,
        "seq": 0,
        "state": {},
    }
    values.update(overrides)
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError, match=constraint):
            await session.execute(insert(RealtimeDoc.__table__).values(**values))


async def test_realtime_docs_foreign_keys_are_named_and_cascade_or_null_as_documented() -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname, confdeltype::text FROM pg_constraint "
                    "WHERE conrelid = 'realtime_docs'::regclass AND contype = 'f' ORDER BY conname"
                )
            )
        ).all()
    assert [(name, action) for name, action in rows] == [
        ("fk_realtime_docs_org_id_teams", "c"),
        ("fk_realtime_docs_owner_user_id_users", "n"),
        ("fk_realtime_docs_team_id_teams", "n"),
    ]


async def test_realtime_presence_round_trips_and_cascades_with_the_user() -> None:
    from alkera_core.models import RealtimePresence, User

    org_id, user_id = await _org_and_user()
    doc_id = f"sess-{uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(
            RealtimePresence(
                doc_type="chat", doc_id=doc_id, peer_id="p:1", user_id=user_id, org_id=org_id
            )
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        row = await session.get(RealtimePresence, ("chat", doc_id, "p:1"))
        assert row is not None
        assert row.joined_at.tzinfo is not None and row.last_seen_at.tzinfo is not None
        user = await session.get(User, user_id)
        assert user is not None
        await session.delete(user)
        await session.commit()
    async with AsyncSessionLocal() as session:
        assert await session.get(RealtimePresence, ("chat", doc_id, "p:1")) is None


async def test_realtime_presence_has_no_org_foreign_key() -> None:
    from alkera_core.models import RealtimePresence

    assert {fk.column.table.name for fk in RealtimePresence.__table__.foreign_keys} == {"users"}


async def test_the_realtime_tables_carry_their_named_indexes() -> None:
    async with AsyncSessionLocal() as session:
        names = set(
            (
                await session.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename IN "
                        "('realtime_docs', 'realtime_presence', 'ws_ticket_uses')"
                    )
                )
            ).scalars()
        )
    assert {
        "ix_realtime_docs_org_id",
        "ix_realtime_docs_owner_user_id",
        "ix_realtime_presence_last_seen_at",
        "ix_realtime_presence_user_id",
        "ix_ws_ticket_uses_consumed_at",
        "pk_ws_ticket_uses",
        "pk_realtime_docs",
        "pk_realtime_presence",
    } <= names
