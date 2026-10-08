"""The last one-per-person rows become one per person per org: the revision,
its back-fills, its fill trigger and its round trip.

``alembic check`` sees the new column, tables and keys. It does not see that
every existing inventory row is bound to its reporter's home org, that the
catalog-scoped preferences are copied into the home org and nothing else is,
that an old writer naming no org still lands in the right one, or that the
downgrade refuses rather than silently dropping a person's second spare. This
module drives the revision on a scratch copy against rows written the way the
previous schema writes them: down, up, re-run, down, up.
"""

from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = [pytest.mark.asyncio]

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0189_multi_org_per_org_rows.py"
_PARENT = "0188"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _org(session: AsyncSession) -> uuid.UUID:
    org = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": org, "name": f"o-{org.hex[:8]}"},
    )
    return org


async def _user(session: AsyncSession, org: uuid.UUID) -> uuid.UUID:
    user = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": user, "org": org, "email": f"per-org-{secrets.token_hex(6)}@alkera.dev"},
    )
    return user


async def _member(session: AsyncSession, user: uuid.UUID, org: uuid.UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO org_memberships (id, user_id, org_team_id) VALUES (:id, :u, :o) "
            "ON CONFLICT DO NOTHING"
        ),
        {"id": uuid.uuid4(), "u": user, "o": org},
    )


async def _inventory(session: AsyncSession, user: uuid.UUID, workspace: str) -> uuid.UUID:
    """An inventory row written as the previous schema writes it: no org."""
    row = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO connection_inventory (id, user_id, workspace_key, plugin) "
            "VALUES (:id, :u, :w, 'postgres')"
        ),
        {"id": row, "u": user, "w": workspace},
    )
    return row


async def _spare(session: AsyncSession, user: uuid.UUID, org: uuid.UUID) -> None:
    obj = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
            "visibility_scope, spec) VALUES (:id, :org, :l, 'chat', :u, 'private', "
            "CAST(:spec AS jsonb))"
        ),
        {"id": obj, "org": org, "l": f"l-{obj.hex[:8]}", "u": user, "spec": '{"spare": true}'},
    )


async def _seed(session: AsyncSession) -> dict[str, Any]:
    home = await _org(session)
    other = await _org(session)
    user = await _user(session, home)
    await _member(session, user, other)
    row = await _inventory(session, user, "ws-1")
    prefs = {
        "schema_version": "1.0.0",
        "theme": "dark",
        "default_chat_model": "claude-x",
        "model_efforts": {"claude-x": "high"},
    }
    await session.execute(
        text("INSERT INTO user_preferences (user_id, preferences) VALUES (:u, CAST(:p AS jsonb))"),
        {"u": user, "p": json.dumps(prefs)},
    )
    plain = await _user(session, home)
    await session.execute(
        text("INSERT INTO user_preferences (user_id, preferences) VALUES (:u, CAST(:p AS jsonb))"),
        {"u": plain, "p": json.dumps({"theme": "light"})},
    )
    await _spare(session, user, home)
    return {"home": home, "other": other, "user": user, "row": row, "plain": plain}


async def test_the_revision_binds_copies_and_round_trips() -> None:
    assert _MIGRATION.is_file()
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            ids = await _seed(session)
            await session.commit()

        await scratch.upgrade()
        async with scratch.session() as session:
            # Bound to the reporter's home org, the only org they could report from.
            assert (
                await _scalar(
                    session,
                    "SELECT org_team_id FROM connection_inventory WHERE id = :i",
                    i=ids["row"],
                )
                == ids["home"]
            )
            assert (
                await _scalar(
                    session,
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'connection_inventory' AND column_name = 'org_team_id'",
                )
                == "NO"
            )
            # Only the catalog-scoped keys move, into the home org only; a
            # document with none of them makes no row.
            copied = (
                await session.execute(
                    text("SELECT user_id, org_team_id, preferences FROM user_org_preferences")
                )
            ).all()
            assert [(r[0], r[1], r[2]) for r in copied] == [
                (
                    ids["user"],
                    ids["home"],
                    {"default_chat_model": "claude-x", "model_efforts": {"claude-x": "high"}},
                )
            ]
            # One spare per person in EACH org: a second org takes a spare,
            # the same org refuses one.
            await _spare(session, ids["user"], ids["other"])
            await session.commit()
            with pytest.raises(DBAPIError) as doubled:
                await _spare(session, ids["user"], ids["home"])
            await session.rollback()
            assert getattr(doubled.value.orig, "sqlstate", None) == "23505"
            # Both keys stand: the previous release's upsert names the old one, so
            # it is kept until a later revision drops it, and until then the same
            # workspace and plugin cannot be reported from a second org either.
            for org in (ids["home"], ids["other"]):
                with pytest.raises(DBAPIError) as same_key:
                    await session.execute(
                        text(
                            "INSERT INTO connection_inventory (id, user_id, org_team_id, "
                            "workspace_key, plugin) VALUES (:id, :u, :o, 'ws-1', 'postgres')"
                        ),
                        {"id": uuid.uuid4(), "u": ids["user"], "o": org},
                    )
                await session.rollback()
                assert getattr(same_key.value.orig, "sqlstate", None) == "23505"
            keys = {
                str(row[0])
                for row in await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'connection_inventory'::regclass AND contype = 'u'"
                    )
                )
            }
            assert keys == {
                "uq_connection_inventory_user_workspace_plugin",
                "uq_connection_inventory_user_org_workspace_plugin",
            }
            assert await _scalar(session, "SELECT to_regclass('sso_link_requests') IS NOT NULL")

        # Re-run over a schema that already has all of it: nothing refuses and
        # nothing is copied twice.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade()
        async with scratch.session() as session:
            assert await _scalar(session, "SELECT count(*) FROM user_org_preferences") == 1

        # The old spare key cannot hold a second spare: the downgrade refuses
        # instead of dropping one.
        with pytest.raises(RuntimeError, match="spare chat"):
            await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            await session.execute(
                text("DELETE FROM workspace_objects WHERE org_team_id = :o"), {"o": ids["other"]}
            )
            await session.commit()

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'connection_inventory' AND column_name = 'org_team_id'",
                )
                == 0
            )
            assert await _scalar(session, "SELECT to_regclass('user_org_preferences')") is None
            assert await _scalar(session, "SELECT to_regclass('sso_link_requests')") is None
            assert await _scalar(
                session, "SELECT to_regclass('uq_workspace_objects_one_spare_per_owner')"
            )
            assert (
                await _scalar(
                    session, "SELECT count(*) FROM connection_inventory WHERE id = :i", i=ids["row"]
                )
                == 1
            )

        await scratch.upgrade()
        async with scratch.session() as session:
            assert (
                await _scalar(
                    session,
                    "SELECT org_team_id FROM connection_inventory WHERE id = :i",
                    i=ids["row"],
                )
                == ids["home"]
            )


async def test_an_old_writer_that_names_no_org_lands_in_the_reporters_home_org() -> None:
    """A backend task from before the column inserts without an org during a
    rolling deploy; the fill trigger binds it to the reporter's home org, and a
    writer that names an org keeps the one it named."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            home = await _org(session)
            other = await _org(session)
            user = await _user(session, home)
            row = await _inventory(session, user, "ws-old")
            named = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO connection_inventory (id, user_id, org_team_id, workspace_key, "
                    "plugin) VALUES (:id, :u, :o, 'ws-named', 'postgres')"
                ),
                {"id": named, "u": user, "o": other},
            )
            await session.commit()
            assert (
                await _scalar(
                    session, "SELECT org_team_id FROM connection_inventory WHERE id = :i", i=row
                )
                == home
            )
            assert (
                await _scalar(
                    session, "SELECT org_team_id FROM connection_inventory WHERE id = :i", i=named
                )
                == other
            )
