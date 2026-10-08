"""Revision 0178 (workspaces) against the real schema.

The upgrade only widens the table: a task of the previous build cannot list a
workspace row, so the revision makes none and every existing chat is adopted
afterwards by the reconcile pass, once the deploy turns adoption on. The
chats are seeded as the schema at 0177 holds them: a chat with a folder, an
old pointer-node chat, a Slack chat and a spare are all live chats, and a
tombstoned chat is not. The pass must give each live one exactly one
workspace that names it, carries its audience and creation time, and owns no
folder, and must leave the chat's folder exactly where it was.

The downgrade takes every workspace and every chat's reference away, and it
completes even while the application keeps adopting in the middle of it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.names import name_key
from alkera_core.models import User, WorkspaceObject
from alkera_core.objects import workspace_reconcile
from backend import migration_safety
from backend.services import workspaces
from backend.services.workspaces.forget import Forgotten, forget_workspaces
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401 -- a fixture this module uses
from tests.conftest import login
from tests.migration_harness import ScratchDatabase, migration_scratch, seed_org_admin
from tests.test_files_share_uniqueness_migration import _seed_drive

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0178_workspaces.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0177"


async def _seed_chat(
    session: AsyncSession,
    *,
    org: uuid.UUID,
    owner: uuid.UUID,
    title: str,
    spec: dict[str, Any] | None = None,
    deleted_at: float = 0,
) -> uuid.UUID:
    chat_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, type, title, "
            "version, status, spec, owner_user_id, visibility_scope, deleted_at, created_at) "
            "VALUES (:id, :org, :logical, 'workspace', 'chat', :title, 1, 'ready', "
            "CAST(:spec AS jsonb), :owner, 'private', :deleted, "
            "now() - interval '3 days')"
        ),
        {
            "id": chat_id,
            "org": org,
            "logical": str(uuid.uuid4()),
            "title": title,
            "spec": json.dumps(spec or {"schema_version": "1.9.0", "machine_status": "none"}),
            "owner": owner,
            "deleted": deleted_at,
        },
    )
    return chat_id


async def _workspaces_of(session: AsyncSession, chat_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT id, org_team_id, owner_user_id, namespace, logical_id, title, spec, "
            "visibility_scope, created_at, deleted_at FROM workspace_objects "
            "WHERE type = 'workspace' AND spec ->> 'adopted_chat_id' = :chat"
        ),
        {"chat": str(chat_id)},
    )
    return [dict(row._mapping) for row in rows]


async def _chat_spec(session: AsyncSession, chat_id: uuid.UUID) -> dict[str, Any]:
    value = await session.scalar(
        text("SELECT spec FROM workspace_objects WHERE id = :id"), {"id": chat_id}
    )
    assert isinstance(value, dict)
    return value


async def _workspace_rows(session: AsyncSession, org: uuid.UUID) -> int:
    """How many workspaces ``org`` has. The test's own org, never the whole
    table: the scratch is a copy of a database other modules wrote to."""
    count = await session.scalar(
        text(
            "SELECT count(*) FROM workspace_objects WHERE type = 'workspace' AND org_team_id = :org"
        ),
        {"org": org},
    )
    return int(count or 0)


async def _rows_an_earlier_module_left(db: ScratchDatabase) -> None:
    """Another org with a live, adopted chat: what a reused database holds
    when this module's turn comes. Nothing here is the test's to count."""
    other = await seed_org_admin(db)
    async with db.session() as session:
        await _seed_chat(session, org=other.org_id, owner=other.admin_id, title="Someone else's")
        await session.commit()
    await _drain(db)


async def _drain(db: ScratchDatabase) -> workspace_reconcile.Reconciled:
    return await workspace_reconcile.drain(db.session, now=datetime.now(UTC), adopt=True)


async def test_the_upgrade_makes_no_workspace_and_the_reconcile_adopts_every_live_chat() -> None:
    async with migration_scratch() as db:
        await _rows_an_earlier_module_left(db)
        org = await seed_org_admin(db)
        await db.downgrade(_PARENT)
        async with db.session() as session:
            folder_chat = await _seed_chat(
                session, org=org.org_id, owner=org.admin_id, title="Weekly mentions"
            )
            pointer_chat = await _seed_chat(
                session, org=org.org_id, owner=org.admin_id, title="Older, a pointer node"
            )
            slack_chat = await _seed_chat(
                session, org=org.org_id, owner=org.admin_id, title="#sales (2)"
            )
            spare = await _seed_chat(
                session,
                org=org.org_id,
                owner=org.admin_id,
                title="",
                spec={"schema_version": "1.9.0", "spare": True},
            )
            gone = await _seed_chat(
                session, org=org.org_id, owner=org.admin_id, title="Deleted", deleted_at=1.0
            )
            created_at = await session.scalar(
                text("SELECT created_at FROM workspace_objects WHERE id = :id"),
                {"id": folder_chat},
            )
            await session.commit()

        await db.upgrade()

        live = (folder_chat, pointer_chat, slack_chat, spare)
        async with db.session() as session:
            # Tasks of the previous build are still serving right after the
            # revision runs, and they cannot list a workspace row.
            assert await _workspace_rows(session, org.org_id) == 0
            for chat_id in live:
                assert "workspace_id" not in await _chat_spec(session, chat_id)

        await _drain(db)

        async with db.session() as session:
            # One workspace per live chat of this org, and no other.
            assert await _workspace_rows(session, org.org_id) == len(live)
            for chat_id in live:
                (workspace,) = await _workspaces_of(session, chat_id)
                assert workspace["namespace"] == "workspace_of_chat"
                assert workspace["logical_id"] == str(chat_id)
                assert workspace["owner_user_id"] == org.admin_id
                assert workspace["visibility_scope"] == "private"
                assert workspace["deleted_at"] == 0
                assert workspace["spec"]["layout"] == "adopted"
                assert workspace["spec"]["kind"] == "project"
                spec = await _chat_spec(session, chat_id)
                assert spec["workspace_id"] == str(workspace["id"])
                # Nothing a box reads off the chat moved.
                assert spec["schema_version"] == "1.9.0"
            (folder_ws,) = await _workspaces_of(session, folder_chat)
            assert folder_ws["title"] == "Weekly mentions"
            assert folder_ws["created_at"] == created_at
            assert await _workspaces_of(session, gone) == []
            assert "workspace_id" not in await _chat_spec(session, gone)
            # It owns no folder: the chat's folder is the workspace's.
            owned = await session.scalar(
                text(
                    "SELECT count(*) FROM file_nodes WHERE target_object_id IN "
                    "(SELECT id FROM workspace_objects "
                    "WHERE type = 'workspace' AND org_team_id = :org)"
                ),
                {"org": org.org_id},
            )
            assert owned == 0

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _workspace_rows(session, org.org_id) == 0
            for chat_id in live:
                assert "workspace_id" not in await _chat_spec(session, chat_id)
            index = await session.scalar(
                text("SELECT to_regclass('ix_workspace_objects_chat_workspace')")
            )
            assert index is None

        # And back up: the pass finds every chat unadopted again.
        await db.upgrade()
        await _drain(db)
        async with db.session() as session:
            for chat_id in live:
                assert len(await _workspaces_of(session, chat_id)) == 1


async def test_a_second_pass_adopts_nothing_twice_and_relinks_a_lost_reference() -> None:
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        await db.downgrade(_PARENT)
        async with db.session() as session:
            chat_id = await _seed_chat(session, org=org.org_id, owner=org.admin_id, title="Once")
            await session.commit()
        await db.upgrade()
        await _drain(db)
        async with db.session() as session:
            (first,) = await _workspaces_of(session, chat_id)
            # A chat whose reference was lost (a hand edit, a stale writer) is
            # re-linked to the workspace that exists, never given a second.
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'workspace_id' WHERE id = :id"),
                {"id": chat_id},
            )
            await session.commit()

        assert (await _drain(db)).adopted == 1
        assert (await _drain(db)).adopted == 0

        async with db.session() as session:
            again = await _workspaces_of(session, chat_id)
            assert [row["id"] for row in again] == [first["id"]]
            assert (await _chat_spec(session, chat_id))["workspace_id"] == str(first["id"])


def _in_a_thread_of_its_own(work: Any) -> None:
    """Run ``work()`` (a coroutine factory) to completion on a loop and thread
    of its own: the revision runs synchronously on the test's loop, so a
    concurrent writer has to be somewhere else."""
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(work())
        except BaseException as exc:  # handed back to the test below
            failure.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]


async def _give_the_application_its_user_columns(db: ScratchDatabase) -> None:
    """The application in this test is the current code, which reads every
    column the current ``User`` model maps. Revisions after this one added some
    of them to ``users`` (the account lifecycle's ``deleted_at``). This
    revision's downgrade never touches that table, so they are put back,
    nullable, for the application to read."""
    async with db.session() as session:
        have = set(
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'users'"
                    )
                )
            ).scalars()
        )
        for column in User.__table__.columns:
            if column.name in have:
                continue
            kind = column.type.compile(dialect=postgresql.dialect())
            await session.execute(text(f'ALTER TABLE users ADD COLUMN "{column.name}" {kind}'))
        await session.commit()


async def test_the_downgrade_completes_while_the_application_keeps_adopting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator forgot to turn adoption off. Between the downgrade's
    batched delete and its CHECK swap the application heals a stray chat on a
    read, adopts a chat it just created, and the reconcile pass runs: the
    downgrade must still complete, leave no workspace row and no chat naming
    one, and land the narrower CHECK validated."""
    monkeypatch.setattr(settings, "workspaces_adoption_enabled", True)
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        await db.downgrade(_REVISION)
        await _give_the_application_its_user_columns(db)
        async with db.session() as session:
            adopted_before = await _seed_chat(
                session, org=org.org_id, owner=org.admin_id, title="Adopted"
            )
            stray = await _seed_chat(session, org=org.org_id, owner=org.admin_id, title="Stray")
            await session.commit()
        await workspace_reconcile.drain(db.session, now=datetime.now(UTC), adopt=True)
        async with db.session() as session:
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'workspace_id' WHERE id = :id"),
                {"id": stray},
            )
            await session.execute(
                text("DELETE FROM workspace_objects WHERE type = 'workspace' AND logical_id = :c"),
                {"c": str(stray)},
            )
            await session.commit()

        made_mid_downgrade: list[uuid.UUID] = []

        async def _the_application_meanwhile() -> None:
            async with AsyncSessionLocal() as session:
                owner = await session.get(User, org.admin_id)
                assert owner is not None
                # A read heals the stray it lists.
                listed = await session.get(WorkspaceObject, stray)
                assert listed is not None
                await workspaces.adopt_strays(session, [listed])
                # A chat created now is adopted in its own transaction.
                fresh = await _seed_chat(
                    session, org=org.org_id, owner=org.admin_id, title="Created meanwhile"
                )
                await session.flush()
                chat = await session.get(WorkspaceObject, fresh)
                assert chat is not None
                assert await workspaces.adopt_chat(session, chat=chat, owner=owner) is not None
                await session.commit()
                made_mid_downgrade.append(fresh)
            async with AsyncSessionLocal() as session:
                await workspace_reconcile.reconcile(session, now=datetime.now(UTC), adopt=True)
                await session.commit()

        batched = migration_safety.in_batches
        fired: list[str] = []

        def _in_batches(table: str, key: str, statement: str, **kwargs: Any) -> int:
            touched = batched(table, key, statement, **kwargs)
            if "DELETE FROM workspace_objects" in statement and not fired:
                fired.append(statement)
                _in_a_thread_of_its_own(_the_application_meanwhile)
            return touched

        monkeypatch.setattr(migration_safety, "in_batches", _in_batches)
        with db.serving():
            await db.downgrade(_PARENT)

        assert fired, "the downgrade never deleted workspaces a batch at a time"
        assert made_mid_downgrade, "the application never adopted mid-downgrade"
        async with db.session() as session:
            assert await _workspace_rows(session, org.org_id) == 0
            dangling = await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_objects "
                    "WHERE type = 'chat' AND spec ? 'workspace_id'"
                )
            )
            assert dangling == 0
            for chat_id in (adopted_before, stray, *made_mid_downgrade):
                assert "workspace_id" not in await _chat_spec(session, chat_id)
            validated = await session.scalar(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conname = 'ck_workspace_objects_type'"
                )
            )
            assert validated is True
            with pytest.raises(Exception, match="ck_workspace_objects_type"):
                await session.execute(
                    text(
                        "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, "
                        "type, title, version, status, spec, owner_user_id, visibility_scope) "
                        "VALUES (gen_random_uuid(), :org, 'x', 'workspace', 'workspace', 'x', "
                        "1, 'ready', '{}'::jsonb, :owner, 'private')"
                    ),
                    {"org": org.org_id, "owner": org.admin_id},
                )


@pytest.mark.usefixtures("files_on")
async def test_the_new_build_serves_chats_while_the_table_refuses_workspaces(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollback's CHECK has landed and the new build is still serving with
    adoption on: listing a chat with no workspace and starting a new chat both
    answer as before, and the chat is left with no workspace rather than the
    request failing on the refused row."""
    monkeypatch.setattr(settings, "workspaces_adoption_enabled", True)
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    async with migration_scratch() as db:
        await _rows_an_earlier_module_left(db)
        org = await seed_org_admin(db)
        async with db.session() as session:
            stray = await _seed_chat(
                session,
                org=org.org_id,
                owner=org.admin_id,
                title="From before",
                spec={"schema_version": "1.10.0", "machine_status": "none"},
            )
            await session.execute(
                text(
                    "ALTER TABLE workspace_objects ADD CONSTRAINT ck_workspaces_refused "
                    "CHECK (type <> 'workspace') NOT VALID"
                )
            )
            await session.commit()
        with db.serving():
            await login(client, org.admin_email, org.admin_password)
            listed = await client.get("/api/v1/chats")
            created = await client.post("/api/v1/chats", json={"title": "Started now"})
            opened = await client.get(f"/api/v1/chats/{stray}")

        assert listed.status_code == 200, listed.text
        assert str(stray) in {item["id"] for item in listed.json()["items"]}
        assert created.status_code == 201, created.text
        assert created.json()["workspace_id"] is None
        assert opened.status_code == 200, opened.text
        assert opened.json()["workspace_id"] is None
        async with db.session() as session:
            assert await _workspace_rows(session, org.org_id) == 0


async def test_a_database_stamped_past_a_parent_it_never_ran_is_refused() -> None:
    """An earlier build numbered this revision 0177, so a database it migrated
    is stamped 0177 without the crdt source columns. Upgrading must refuse and
    name the repair rather than run this revision alone and leave every live
    document read failing; after the repair both revisions apply."""
    async with migration_scratch() as db:
        await db.downgrade("0176")
        async with db.session() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = '0177'"))
            await session.commit()

        with pytest.raises(Exception, match=r"alembic stamp 0176"):
            await db.upgrade()

        async with db.session() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = '0176'"))
            await session.commit()
        await db.upgrade()
        async with db.session() as session:
            source = await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'crdt_docs' AND column_name = 'source_etag'"
                )
            )
            assert source == 1


async def _seed_native_workspace(
    session: AsyncSession,
    *,
    org: Any,
    workspace_id: uuid.UUID,
    node_id: uuid.UUID,
    drive_id: uuid.UUID,
    name: bytes,
) -> None:
    """A workspace made as one, and the folder it owns."""
    await session.execute(
        text(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, namespace, "
            "type, title, version, status, spec, owner_user_id, visibility_scope) "
            "VALUES (:id, :org, :logical, 'workspace', 'workspace', 'Pricing study', "
            "1, 'ready', CAST(:spec AS jsonb), :owner, 'private')"
        ),
        {
            "id": workspace_id,
            "org": org.org_id,
            "logical": str(uuid.uuid4()),
            "spec": json.dumps({"schema_version": "1.0.0", "kind": "project"}),
            "owner": org.admin_id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, "
            "subtype, target_object_id, name, name_display, name_key, flags_names, "
            "path_ids, depth, mode, uid, gid, nlink, size, rdev, atime_ns, mtime_ns, "
            "ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, metadata) "
            "VALUES (:id, 1, :drive, :org, NULL, 'folder', 'workspace', :target, "
            ":name, :display, :key, '{}'::jsonb, CAST(:path AS ltree), 0, 493, 0, 0, "
            "1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, 0, 0, false, '{}'::jsonb)"
        ),
        {
            "id": node_id,
            "drive": drive_id,
            "org": org.org_id,
            "target": workspace_id,
            "name": name,
            "display": name.decode(),
            "key": name_key(name),
            "path": str(node_id).replace("-", "_"),
        },
    )


async def test_the_downgrade_leaves_a_workspace_folder_as_an_ordinary_folder() -> None:
    """A workspace made as one owns a folder. Rolling back keeps the folder and
    its name, and untags it: the previous build has no workspace type, so the
    folder must name neither the type nor the row the rollback deletes."""
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        workspace_id, node_id = uuid.uuid4(), uuid.uuid4()
        name = b"Pricing study.alkeraworkspace"
        async with db.session() as session:
            drive_id = await _seed_drive(session, org.org_id)
            await _seed_native_workspace(
                session,
                org=org,
                workspace_id=workspace_id,
                node_id=node_id,
                drive_id=drive_id,
                name=name,
            )
            await session.commit()

        await db.downgrade(_PARENT)

        async with db.session() as session:
            node = (
                await session.execute(
                    text("SELECT name, subtype, target_object_id FROM file_nodes WHERE id = :id"),
                    {"id": node_id},
                )
            ).one()
            gone = await session.scalar(
                text("SELECT count(*) FROM workspace_objects WHERE id = :id"),
                {"id": workspace_id},
            )
        assert (bytes(node.name), node.subtype, node.target_object_id) == (name, None, None)
        assert gone == 0


async def test_an_app_only_rollback_takes_every_workspace_away_and_leaves_the_schema() -> None:
    """Rolling the services back while the schema stays at head: a count first
    changes nothing, then every chat's reference, every workspace folder's tag
    and every workspace row go, a batch at a time, and a second run finds
    nothing. The schema still admits workspaces, so rolling forward again is
    only turning adoption back on."""
    async with migration_scratch() as db:
        await _rows_an_earlier_module_left(db)
        org = await seed_org_admin(db)
        workspace_id, node_id = uuid.uuid4(), uuid.uuid4()
        name = b"Pricing study.alkeraworkspace"
        async with db.session() as session:
            # The tool counts the whole database, so the copy is made to hold
            # this test's workspaces and chats only: every other org's chat is
            # tombstoned, which the adoption pass and the tool both pass over.
            await session.execute(
                text(
                    "UPDATE workspace_objects SET deleted_at = 1 "
                    "WHERE type = 'chat' AND deleted_at = 0 AND org_team_id <> :org"
                ),
                {"org": org.org_id},
            )
            await session.execute(text("DELETE FROM workspace_objects WHERE type = 'workspace'"))
            await session.execute(
                text("UPDATE file_nodes SET subtype = NULL WHERE subtype = 'workspace'")
            )
            await session.execute(
                text(
                    "UPDATE workspace_objects SET spec = spec - 'workspace_id' WHERE type = 'chat'"
                )
            )
            chats = [
                await _seed_chat(session, org=org.org_id, owner=org.admin_id, title=f"c{i}")
                for i in range(3)
            ]
            drive_id = await _seed_drive(session, org.org_id)
            await _seed_native_workspace(
                session,
                org=org,
                workspace_id=workspace_id,
                node_id=node_id,
                drive_id=drive_id,
                name=name,
            )
            await session.commit()
        await _drain(db)

        counted = await forget_workspaces(db.session, apply=False)
        assert counted == Forgotten(chats=3, folders=1, workspaces=4)
        assert await forget_workspaces(db.session, apply=False) == counted, (
            "a count changes nothing"
        )

        done = await forget_workspaces(db.session, apply=True, batch=2)

        assert done == counted
        async with db.session() as session:
            assert await _workspace_rows(session, org.org_id) == 0
            for chat_id in chats:
                assert "workspace_id" not in await _chat_spec(session, chat_id)
            node = (
                await session.execute(
                    text("SELECT name, subtype, target_object_id FROM file_nodes WHERE id = :id"),
                    {"id": node_id},
                )
            ).one()
            assert (bytes(node.name), node.subtype, node.target_object_id) == (name, None, None)
        assert await forget_workspaces(db.session, apply=True) == Forgotten(0, 0, 0)

        await _drain(db)
        async with db.session() as session:
            for chat_id in chats:
                assert len(await _workspaces_of(session, chat_id)) == 1
