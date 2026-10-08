"""Row-level security on the content tables: the revision, its back-fills, its
triggers and its round trip.

``alembic check`` sees the three new columns and their indexes. It sees neither
the back-fill that names every existing row's org, nor the policies, the role
and its grants, nor the triggers that keep every writer that predates the
column correct while the old backend tasks of a rolling deploy still run. This
module drives the revision on a scratch copy against rows written the way the
previous schema writes them: down, up, re-run, down, up.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alkera_core.db.row_security import CONTENT_TABLES, TENANT_APP_ROLE
from alkera_core.db.tenant_session import TENANT_ROLE
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = [pytest.mark.asyncio]

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0188_content_row_security.py"
_PARENT = "0187"
_BOUND = ("team_connections", "user_oauth_tokens", "object_payload_rows")


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _team(session: AsyncSession, parent: uuid.UUID | None = None) -> uuid.UUID:
    team = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO teams (id, name, is_root, parent_team_id) "
            "VALUES (:id, :name, :root, :parent)"
        ),
        {"id": team, "name": f"t-{team.hex[:8]}", "root": parent is None, "parent": parent},
    )
    return team


async def _user(session: AsyncSession, org: uuid.UUID) -> uuid.UUID:
    user = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": user, "org": org, "email": f"rls-{secrets.token_hex(6)}@alkera.dev"},
    )
    return user


async def _old_rows(session: AsyncSession) -> dict[str, uuid.UUID]:
    """A connection on a sub-team, a grant on it and an object's payload page,
    written as the previous schema writes them: no org on any of the three."""
    org = await _team(session)
    sub = await _team(session, org)
    user = await _user(session, org)
    connection = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO team_connections (id, team_id, plugin, handle) "
            "VALUES (:id, :team, 'postgres', :h)"
        ),
        {"id": connection, "team": sub, "h": f"h-{connection.hex[:8]}"},
    )
    grant = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO user_oauth_tokens (id, user_id, team_connection_id, "
            "access_token_encrypted) VALUES (:id, :u, :c, 'x')"
        ),
        {"id": grant, "u": user, "c": connection},
    )
    obj = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
            "visibility_scope) VALUES (:id, :org, :l, 'chat', :u, 'org')"
        ),
        {"id": obj, "org": org, "l": f"l-{obj.hex[:8]}", "u": user},
    )
    await session.execute(
        text(
            "INSERT INTO object_payload_rows (object_id, page, sha256, size) "
            "VALUES (:o, 0, :sha, 0)"
        ),
        {"o": obj, "sha": "0" * 64},
    )
    return {
        "org": org,
        "sub": sub,
        "user": user,
        "connection": connection,
        "grant": grant,
        "object": obj,
    }


def _revision(path: Path, name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The tables this revision polices; a later revision polices its own tables.
_REV_TABLES: dict[str, str] = dict(_revision(_MIGRATION, "rev_0188_tables").TABLES)
#: Every later revision that polices tables of its own.
_LATER = (
    _BACKEND / "alembic" / "versions" / "0196_org_machines.py",
    _BACKEND / "alembic" / "versions" / "0202_notebooks.py",
    _BACKEND / "alembic" / "versions" / "0203_notebook_peers_and_tails.py",
    _BACKEND / "alembic" / "versions" / "0222_ssh_machines.py",
)


async def _policed(session: AsyncSession) -> dict[str, tuple[bool, bool, bool]]:
    rows = await session.execute(
        text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
            "EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid "
            "        AND p.polname = 'tenant_isolation') "
            "FROM pg_class c WHERE c.relname = ANY(:names) AND c.relkind = 'r'"
        ),
        {"names": list(_REV_TABLES)},
    )
    return {str(r[0]): (r[1], r[2], r[3]) for r in rows}


async def _orgs_of(session: AsyncSession, ids: dict[str, uuid.UUID]) -> dict[str, Any]:
    return {
        "team_connections": await _scalar(
            session, "SELECT org_team_id FROM team_connections WHERE id = :i", i=ids["connection"]
        ),
        "user_oauth_tokens": await _scalar(
            session, "SELECT org_team_id FROM user_oauth_tokens WHERE id = :i", i=ids["grant"]
        ),
        "object_payload_rows": await _scalar(
            session,
            "SELECT org_team_id FROM object_payload_rows WHERE object_id = :i",
            i=ids["object"],
        ),
    }


async def test_the_revision_binds_every_row_polices_every_table_and_round_trips() -> None:
    assert _MIGRATION.is_file()
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            ids = await _old_rows(session)
            await session.commit()

        await scratch.upgrade()
        async with scratch.session() as session:
            # Back-filled from where each row hangs: a sub-team's connection
            # belongs to the org at its root.
            assert await _orgs_of(session, ids) == dict.fromkeys(_BOUND, ids["org"])
            assert await _policed(session) == dict.fromkeys(_REV_TABLES, (True, True, True))
            role = (
                await session.execute(
                    text(
                        "SELECT rolcanlogin, rolsuper, rolbypassrls, "
                        "pg_has_role(current_user, oid, 'MEMBER') "
                        "FROM pg_roles WHERE rolname = :r"
                    ),
                    {"r": TENANT_APP_ROLE},
                )
            ).one()
            assert tuple(role) == (False, False, False, True)
            for table in _BOUND:
                nullable = await _scalar(
                    session,
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'org_team_id'",
                    t=table,
                )
                assert nullable == "NO", table

        # Re-run over a schema that already has all of it: nothing refuses.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade()
        async with scratch.session() as session:
            assert await _orgs_of(session, ids) == dict.fromkeys(_BOUND, ids["org"])
            assert await _policed(session) == dict.fromkeys(_REV_TABLES, (True, True, True))

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _policed(session) == dict.fromkeys(_REV_TABLES, (False, False, False))
            for table in _BOUND:
                assert (
                    await _scalar(
                        session,
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = 'org_team_id'",
                        t=table,
                    )
                    == 0
                ), table
            assert await _scalar(session, "SELECT to_regprocedure('alkera_org_ids()')") is None
            # The rows themselves survive the trip.
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM team_connections WHERE id = :i",
                    i=ids["connection"],
                )
                == 1
            )

        await scratch.upgrade()
        async with scratch.session() as session:
            assert await _orgs_of(session, ids) == dict.fromkeys(_BOUND, ids["org"])


async def test_an_old_writer_that_names_no_org_gets_the_right_one_and_a_change_is_refused() -> None:
    """The triggers keep a writer that predates the column correct, refuse a
    value that disagrees with where the row hangs, and refuse any change."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            ids = await _old_rows(session)
            await session.commit()
            assert await _orgs_of(session, ids) == dict.fromkeys(_BOUND, ids["org"])

            other = await _team(session)
            await session.commit()
            refusals = [
                (
                    "UPDATE team_connections SET org_team_id = :o WHERE id = :i",
                    ids["connection"],
                ),
                ("UPDATE user_oauth_tokens SET org_team_id = :o WHERE id = :i", ids["grant"]),
                (
                    "UPDATE object_payload_rows SET org_team_id = :o WHERE object_id = :i",
                    ids["object"],
                ),
                # A connection moved to a team in another org would take its
                # org along; the trigger refuses the move instead.
                ("UPDATE team_connections SET team_id = :o WHERE id = :i", ids["connection"]),
            ]
            for statement, row in refusals:
                with pytest.raises(DBAPIError) as refused:
                    await session.execute(text(statement), {"o": other, "i": row})
                await session.rollback()
                assert getattr(refused.value.orig, "sqlstate", None) == "23514", statement

            # A writer naming the wrong org outright is refused at insert.
            with pytest.raises(DBAPIError) as wrong:
                await session.execute(
                    text(
                        "INSERT INTO team_connections (id, team_id, org_team_id, plugin, handle) "
                        "VALUES (:id, :team, :org, 'postgres', 'wrong')"
                    ),
                    {"id": uuid.uuid4(), "team": ids["sub"], "org": other},
                )
            await session.rollback()
            assert getattr(wrong.value.orig, "sqlstate", None) == "23514"


async def test_the_revision_refuses_a_login_that_cannot_bypass_row_security() -> None:
    """Forcing the policies under a login that does not bypass row security
    would make every content table read as empty for the worker and the old
    tasks of the roll. The refusal comes before anything changes."""
    from backend.migration_safety import RowSecurityBlocksMigrationError

    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        connection = scratch.config.attributes["connection"]
        login = f"rls_nobypass_{secrets.token_hex(4)}"
        connection.execute(text(f"CREATE ROLE {login} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
        connection.execute(text(f"GRANT ALL ON SCHEMA public TO {login}"))
        connection.execute(text(f"GRANT SELECT, UPDATE ON alembic_version TO {login}"))
        connection.execute(text(f"SET ROLE {login}"))
        connection.commit()
        try:
            with pytest.raises(RowSecurityBlocksMigrationError):
                command.upgrade(scratch.config, "head")
        finally:
            connection.rollback()
            connection.execute(text("RESET ROLE"))
            connection.execute(text(f"REVOKE ALL ON alembic_version FROM {login}"))
            connection.execute(text(f"REVOKE ALL ON SCHEMA public FROM {login}"))
            connection.execute(text(f"DROP ROLE {login}"))
            connection.commit()
        assert scratch.revision() == _PARENT


def test_the_revision_and_the_runtime_agree_on_the_tables_and_the_role() -> None:
    """Every content table is policed by exactly one revision, and those
    revisions together police exactly the runtime's list, under its role."""
    revisions = [_revision(_MIGRATION, "rev_0188")] + [
        _revision(path, f"rev_{path.stem}") for path in _LATER
    ]
    named = [table for revision in revisions for table, _ in revision.TABLES]
    assert len(named) == len(set(named))
    assert {
        table: column for revision in revisions for table, column in revision.TABLES
    } == CONTENT_TABLES
    assert all(revision.ROLE == TENANT_APP_ROLE == TENANT_ROLE for revision in revisions)
