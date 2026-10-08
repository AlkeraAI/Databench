"""The three new tables, exercised against the real schema.

A CHECK constraint that is not tested is a comment: it either fires or it does
not, and nobody finds out until a bad row is already there. So every constraint
the migration writes is driven here with a row that should be refused, and the
vocabularies are pinned equal across the model and the migration so a value
added on one side cannot be forgotten on the other.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events.outbox import MAX_PAYLOAD_BYTES
from alkera_core.models import ChatMessage, ObjectPayloadRow, User, WorkspaceObject
from alkera_core.models.event_outbox import _MAX_PAYLOAD_BYTES as _OUTBOX_MAX_PAYLOAD_BYTES
from alkera_core.models.workspace_object import (
    MAX_MESSAGE_PAYLOAD_BYTES,
    MESSAGE_ROLES,
    OBJECT_STATUSES,
    OBJECT_TYPES,
)
from backend.services.objects import object_service
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION = VERSIONS / "0104_workspace_objects.py"
#: The revision that last moved the payload CHECK, and so the authority on what
#: the database admits today.
PAYLOAD_MIGRATION = VERSIONS / "0141_chat_payload_ceiling.py"
#: Every revision that widened ``ck_workspace_objects_type``, oldest first. The
#: last one is the authority on what the database admits today; the ones before
#: it are kept so the chain can be walked and a skipped link shows up.
TYPE_MIGRATIONS: tuple[Path, ...] = (
    VERSIONS / "0118_report_objects.py",
    VERSIONS / "0130_chat_template_objects.py",
    VERSIONS / "0178_workspaces.py",
)


def _type_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0104", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload_migration() -> ModuleType:
    return _type_migration(PAYLOAD_MIGRATION)


async def _admin(db: AsyncSession, org: OrgWithAdmin) -> User:
    user = await db.get(User, org.admin_id)
    assert user is not None
    return user


async def _object(db: AsyncSession, owner: User, **kwargs: Any) -> WorkspaceObject:
    obj, _ = await object_service.create_object(
        db,
        owner=owner,
        type="result",
        title="Rows",
        spec={},
        **kwargs,
        org_id=owner.home_org_team_id,
    )
    await db.commit()
    return obj


# ---------------------------------------------------------------------------
# The vocabularies live in two files and must never disagree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_values", "attribute"),
    [
        pytest.param(OBJECT_STATUSES, "OBJECT_STATUSES", id="object_statuses"),
        pytest.param(MESSAGE_ROLES, "MESSAGE_ROLES", id="message_roles"),
    ],
)
def test_the_migration_and_the_model_spell_the_same_vocabulary(
    model_values: tuple[str, ...], attribute: str
) -> None:
    assert getattr(_migration(), attribute) == model_values


def test_the_type_vocabulary_is_the_one_the_latest_migration_admits() -> None:
    """``type`` is the one vocabulary that has been widened since ``0104``.

    The authority is therefore the migration that last changed the CHECK, not
    the one that created it: pinning against ``0104`` would say the model may
    not store a ``report``, which the database is perfectly happy to accept.
    Whenever another word is admitted, it joins ``TYPE_MIGRATIONS``.
    """
    widenings = [_type_migration(path) for path in TYPE_MIGRATIONS]
    assert widenings[-1]._AFTER == OBJECT_TYPES
    # The chain is unbroken: each widening starts from what the one before it
    # left, and the first from what 0104 created. A revision that widened the
    # CHECK without being listed here breaks this rather than passing unseen.
    previous = _migration().OBJECT_TYPES
    for widening in widenings:
        assert widening._BEFORE == previous
        # A widening only ever adds; it never drops a word already stored.
        assert set(widening._AFTER) > set(previous)
        previous = widening._AFTER
    assert set(OBJECT_TYPES) - set(_migration().OBJECT_TYPES) == {
        "report",
        "chat_template",
        "workspace",
    }


def test_the_message_payload_cap_is_the_same_number_everywhere_it_is_spelled() -> None:
    """Four spellings of one number, and the authority is the migration that
    last moved it — not ``0104``, which created the CHECK at a cap the database
    has since outgrown.

    A transcript entry is ALSO published as an event, so the outbox's cap is the
    same number by necessity: a message the transcript accepts and the event log
    refuses is a message recorded for its author and delivered to nobody. And
    the cap must stay under the document ceiling, because a chat document holds
    the op that carried the message.
    """
    assert _payload_migration().MAX_PAYLOAD_BYTES == MAX_MESSAGE_PAYLOAD_BYTES
    assert MAX_MESSAGE_PAYLOAD_BYTES == MAX_PAYLOAD_BYTES == _OUTBOX_MAX_PAYLOAD_BYTES
    assert MAX_MESSAGE_PAYLOAD_BYTES < settings.realtime_doc_max_bytes
    # 0104 is the pre-image, kept so the widening is visibly a widening.
    assert _migration().MAX_MESSAGE_PAYLOAD_BYTES < MAX_MESSAGE_PAYLOAD_BYTES


# ---------------------------------------------------------------------------
# workspace_objects
# ---------------------------------------------------------------------------


async def test_an_object_round_trips_with_its_defaults(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _object(real_session, admin)
    async with AsyncSessionLocal() as db:
        stored = await db.get(WorkspaceObject, obj.id)
    assert stored is not None
    assert stored.version == 1
    assert stored.status == "ready"
    assert stored.namespace == "workspace"
    assert stored.deleted_at == 0
    assert stored.spec == {}
    assert stored.created_at is not None and stored.updated_at is not None


async def test_two_objects_cannot_share_a_logical_id_in_one_namespace(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    logical = f"dup-{uuid4().hex[:8]}"
    await _object(real_session, admin, logical_id=logical)
    async with AsyncSessionLocal() as db:
        db.add(
            WorkspaceObject(
                id=uuid4(),
                org_team_id=org_admin.org_id,
                logical_id=logical,
                namespace="workspace",
                type="query",
                title="Clash",
                status="ready",
                spec={},
                owner_user_id=org_admin.admin_id,
                visibility_scope="org",
            )
        )
        with pytest.raises(IntegrityError, match="uq_workspace_objects_org_namespace_logical"):
            await db.commit()


async def test_two_orgs_may_use_the_same_logical_id(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The uniqueness is per org; a client id chosen by one tenant must not
    stop another tenant using the same one."""
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, _unique_org_name

    admin = await _admin(real_session, org_admin)
    logical = f"shared-{uuid4().hex[:8]}"
    await _object(real_session, admin, logical_id=logical)
    async with AsyncSessionLocal() as db:
        _org, other_admin = await team_service.create_org_with_admin(
            db,
            org_name=_unique_org_name(),
            admin_email=_unique_email("neighbour"),
            admin_first_name="N",
            admin_last_name="B",
            admin_password="neighbour-pass-12345",
        )
        await db.commit()
        second, created = await object_service.create_object(
            db,
            owner=other_admin,
            type="query",
            title="Mine",
            spec={},
            logical_id=logical,
            org_id=other_admin.home_org_team_id,
        )
        await db.commit()
    assert created is True
    assert second.logical_id == logical


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        pytest.param("type", "spreadsheet", "ck_workspace_objects_type", id="unknown_type"),
        pytest.param("status", "half", "ck_workspace_objects_status", id="unknown_status"),
        pytest.param("version", 0, "ck_workspace_objects_version_positive", id="version_zero"),
    ],
)
async def test_workspace_object_check_constraints(
    org_admin: OrgWithAdmin, column: str, value: object, constraint: str
) -> None:
    async with AsyncSessionLocal() as db:
        fields: dict[str, Any] = {
            "id": uuid4(),
            "org_team_id": org_admin.org_id,
            "logical_id": f"bad-{uuid4().hex[:8]}",
            "namespace": "workspace",
            "type": "result",
            "title": "Bad",
            "status": "ready",
            "version": 1,
            "spec": {},
            "owner_user_id": org_admin.admin_id,
            "visibility_scope": "org",
            column: value,
        }
        db.add(WorkspaceObject(**fields))
        with pytest.raises(IntegrityError, match=constraint):
            await db.commit()


@pytest.mark.parametrize("object_type", list(OBJECT_TYPES), ids=str)
async def test_the_database_accepts_every_type_the_model_declares(
    org_admin: OrgWithAdmin, object_type: str
) -> None:
    """The other half of the CHECK: a word the model admits, the column takes.

    The refusal case above proves the constraint fires; without this one a
    migration that narrowed the vocabulary — or a type added to the tuple with
    no migration at all — would still pass, and the failure would surface as a
    500 the first time someone saved a report.
    """
    async with AsyncSessionLocal() as db:
        db.add(
            WorkspaceObject(
                id=uuid4(),
                org_team_id=org_admin.org_id,
                logical_id=f"ok-{uuid4().hex[:8]}",
                namespace="workspace",
                type=object_type,
                title="Admitted",
                status="ready",
                version=1,
                spec={},
                owner_user_id=org_admin.admin_id,
                visibility_scope="org",
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# chat_messages
# ---------------------------------------------------------------------------


async def _chat(db: AsyncSession, owner: User) -> WorkspaceObject:
    obj, _ = await object_service.create_object(
        db, owner=owner, type="chat", title="Ops", spec={}, org_id=owner.home_org_team_id
    )
    await db.commit()
    return obj


def _message(chat_id: UUID, org_id: UUID, **overrides: Any) -> ChatMessage:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "chat_id": chat_id,
        "org_team_id": org_id,
        "seq": 1,
        "role": "user",
        "kind": "prompt",
        "event_id": f"e-{uuid4().hex[:8]}",
        "payload": {"text": "hi"},
    }
    fields.update(overrides)
    return ChatMessage(**fields)


async def test_a_message_round_trips(real_session: AsyncSession, org_admin: OrgWithAdmin) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    async with AsyncSessionLocal() as db:
        db.add(_message(chat.id, org_admin.org_id))
        await db.commit()
        rows = await db.execute(select(ChatMessage).where(ChatMessage.chat_id == chat.id))
        stored = rows.scalar_one()
    assert stored.payload == {"text": "hi"}
    assert stored.created_at is not None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"role": "robot"}, "ck_chat_messages_role", id="unknown_role"),
        pytest.param({"seq": 0}, "ck_chat_messages_seq_positive", id="seq_zero"),
        pytest.param({"seq": -1}, "ck_chat_messages_seq_positive", id="seq_negative"),
        pytest.param(
            {"payload": {"text": "x" * (MAX_MESSAGE_PAYLOAD_BYTES + 100)}},
            "ck_chat_messages_payload_size",
            id="payload_over_the_cap",
        ),
    ],
)
async def test_chat_message_check_constraints(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    async with AsyncSessionLocal() as db:
        db.add(_message(chat.id, org_admin.org_id, **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            await db.commit()


@pytest.mark.parametrize(
    ("second", "constraint"),
    [
        pytest.param({"seq": 1}, "uq_chat_messages_chat_seq", id="duplicate_seq"),
        pytest.param({"seq": 2, "event_id": "same"}, "uq_chat_messages_chat_event", id="dup_event"),
    ],
)
async def test_chat_message_uniqueness(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    second: dict[str, Any],
    constraint: str,
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    async with AsyncSessionLocal() as db:
        db.add(_message(chat.id, org_admin.org_id, seq=1, event_id="same"))
        await db.commit()
        db.add(_message(chat.id, org_admin.org_id, **second))
        with pytest.raises(IntegrityError, match=constraint):
            await db.commit()


async def test_two_chats_may_reuse_a_sequence_and_an_event_id(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The uniqueness is per chat: every conversation starts at seq 1."""
    admin = await _admin(real_session, org_admin)
    first = await _chat(real_session, admin)
    second = await _chat(real_session, admin)
    shared = f"shared-{uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        db.add(_message(first.id, org_admin.org_id, seq=1, event_id=shared))
        db.add(_message(second.id, org_admin.org_id, seq=1, event_id=shared))
        await db.commit()
        rows = await db.execute(select(ChatMessage).where(ChatMessage.event_id == shared))
        assert len(list(rows.scalars().all())) == 2


async def test_a_transcript_goes_with_its_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    chat = await _chat(real_session, admin)
    chat_id = chat.id
    async with AsyncSessionLocal() as db:
        db.add(_message(chat_id, org_admin.org_id))
        await db.commit()
        doomed = await db.get(WorkspaceObject, chat_id)
        assert doomed is not None
        await db.delete(doomed)
        await db.commit()
        rows = await db.execute(select(ChatMessage).where(ChatMessage.chat_id == chat_id))
        assert rows.scalars().all() == []


# ---------------------------------------------------------------------------
# object_payload_rows
# ---------------------------------------------------------------------------


async def test_a_payload_page_round_trips_and_is_keyed_by_page(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _object(real_session, admin)
    async with AsyncSessionLocal() as db:
        db.add(
            ObjectPayloadRow(
                object_id=obj.id, page=0, sha256="a" * 64, size=10, page_rows=[["x", 1]]
            )
        )
        await db.commit()
        stored = await db.get(ObjectPayloadRow, (obj.id, 0))
    assert stored is not None
    assert stored.page_rows == [["x", 1]]
    assert stored.media_type == "application/json"


async def test_two_pages_cannot_share_a_number(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _object(real_session, admin)
    async with AsyncSessionLocal() as db:
        db.add(ObjectPayloadRow(object_id=obj.id, page=0, sha256="a" * 64, size=1, page_rows=[]))
        await db.commit()
    async with AsyncSessionLocal() as db:
        db.add(ObjectPayloadRow(object_id=obj.id, page=0, sha256="b" * 64, size=1, page_rows=[]))
        with pytest.raises(IntegrityError, match="pk_object_payload_rows"):
            await db.commit()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"page": -1}, "ck_object_payload_rows_page_nonnegative", id="negative_page"),
        pytest.param({"size": -1}, "ck_object_payload_rows_size_nonnegative", id="negative_size"),
    ],
)
async def test_payload_row_check_constraints(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _object(real_session, admin)
    fields: dict[str, Any] = {
        "object_id": obj.id,
        "page": 0,
        "sha256": "a" * 64,
        "size": 1,
        "page_rows": [],
        **overrides,
    }
    async with AsyncSessionLocal() as db:
        db.add(ObjectPayloadRow(**fields))
        with pytest.raises(IntegrityError, match=constraint):
            await db.commit()


async def test_payload_pages_go_with_their_object(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    admin = await _admin(real_session, org_admin)
    obj = await _object(real_session, admin)
    object_id = obj.id
    async with AsyncSessionLocal() as db:
        db.add(ObjectPayloadRow(object_id=object_id, page=0, sha256="a" * 64, size=1, page_rows=[]))
        await db.commit()
        doomed = await db.get(WorkspaceObject, object_id)
        assert doomed is not None
        await db.delete(doomed)
        await db.commit()
        rows = await db.execute(
            select(ObjectPayloadRow).where(ObjectPayloadRow.object_id == object_id)
        )
        assert rows.scalars().all() == []
