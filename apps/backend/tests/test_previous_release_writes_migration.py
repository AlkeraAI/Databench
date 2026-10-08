"""The previous release keeps writing while this release's revisions run, and after.

``alembic upgrade head`` runs before the services roll, and the previous
release's tasks keep serving for as long as the roll takes (a backend task
drains for minutes, the worker families roll last), or for good after an
app-only rollback. Everything they write must keep landing: a signup's seat, a
person added to a team, a role change, a connection and its OAuth grant, a
token refresh, an object's payload page, a workspace's connection inventory.

Each revision that names the org on a row that had none (0185, 0187, 0188, 0189)
commits part of itself before its back-fill, so the previous release meets
three schemas: before, part way (column and trigger in, rows not yet named) and
after. This module writes the rows the previous release writes, with the
statements it issues, at every one of them -- the part-way state is the one a
revision leaves when it first leaves its transaction -- and then finishes the
upgrade and checks every row landed in the org it belongs to.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from alembic import op
from backend import migration_safety
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import ScratchDatabase, migration_scratch

#: The schema the previous release was written against: before the first
#: revision of this release that names an org on an existing row.
_PREVIOUS = "0184"
_INVENTORY_KEY = "uq_connection_inventory_user_workspace_plugin"


class _RunDiedError(RuntimeError):
    """The deploy stopped just after the revision first committed part of itself."""


@contextmanager
def _stops_on_leaving_the_transaction() -> Iterator[None]:
    with op.get_context().autocommit_block():
        pass
    raise _RunDiedError("the run stopped as the revision left its transaction")
    yield  # pragma: no cover - never reached; a generator for @contextmanager


@dataclass(frozen=True)
class _Graph:
    """An org as the previous release left it, before any revision of this one."""

    org: uuid.UUID
    sub: uuid.UUID
    person: uuid.UUID
    team_membership: uuid.UUID
    connection: uuid.UUID
    grant: uuid.UUID
    obj: uuid.UUID


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _seed_previous(scratch: ScratchDatabase) -> _Graph:
    ids = {
        name: uuid.uuid4()
        for name in ("org", "sub", "person", "tm", "connection", "grant", "obj", "seat")
    }
    statements = (
        ("INSERT INTO teams (id, name, is_root) VALUES (:org, :org_name, true)", {}),
        (
            "INSERT INTO teams (id, name, is_root, parent_team_id) "
            "VALUES (:sub, :sub_name, false, :org)",
            {},
        ),
        ("INSERT INTO users (id, org_team_id, email) VALUES (:person, :org, :email)", {}),
        (
            "INSERT INTO team_memberships (id, user_id, team_id) VALUES (:tm, :person, :sub)",
            {},
        ),
        (
            "INSERT INTO billing_accounts (id, scope, owner_user_id) "
            "VALUES (:seat, 'user', :person)",
            {},
        ),
        (
            "INSERT INTO team_connections (id, team_id, plugin, handle) "
            "VALUES (:connection, :sub, 'postgres', :handle)",
            {},
        ),
        (
            "INSERT INTO user_oauth_tokens (id, user_id, team_connection_id, "
            "access_token_encrypted) VALUES (:grant, :person, :connection, 'x')",
            {},
        ),
        (
            "INSERT INTO workspace_objects (id, org_team_id, logical_id, type, owner_user_id, "
            "visibility_scope) VALUES (:obj, :org, :logical, 'chat', :person, 'org')",
            {},
        ),
        (
            "INSERT INTO connection_inventory (id, user_id, workspace_key, plugin) "
            "VALUES (gen_random_uuid(), :person, 'ws-1', 'postgres')",
            {},
        ),
    )
    params = {
        **ids,
        "org_name": f"prev-{ids['org'].hex[:8]}",
        "sub_name": f"prev-sub-{ids['sub'].hex[:8]}",
        "email": f"prev-{secrets.token_hex(6)}@alkera.dev",
        "handle": f"h-{ids['connection'].hex[:8]}",
        "logical": f"l-{ids['obj'].hex[:8]}",
    }
    async with scratch.session() as session:
        for sql, extra in statements:
            await session.execute(text(sql), {**params, **extra})
        await session.commit()
    return _Graph(
        org=ids["org"],
        sub=ids["sub"],
        person=ids["person"],
        team_membership=ids["tm"],
        connection=ids["connection"],
        grant=ids["grant"],
        obj=ids["obj"],
    )


@dataclass
class _Written:
    """What the previous release's writes created, to find the rows again."""

    newcomer: uuid.UUID
    connection: uuid.UUID
    grant: uuid.UUID
    seat: uuid.UUID | None = None


async def _previous_release_writes(
    scratch: ScratchDatabase, graph: _Graph
) -> tuple[_Written, dict[str, str]]:
    """Each write the previous release makes, in a transaction of its own, the
    way it issues it. Answers what it wrote and every write that was refused."""
    written = _Written(newcomer=uuid.uuid4(), connection=uuid.uuid4(), grant=uuid.uuid4())
    refused: dict[str, str] = {}

    async def attempt(name: str, sql: str, **params: Any) -> Any:
        async with scratch.session() as session:
            try:
                result = await session.execute(text(sql), params)
                value = result.scalar() if result.returns_rows else None
                await session.commit()
                return value
            except DBAPIError as error:
                refused[name] = str(error.orig).splitlines()[0]
                await session.rollback()
                return None

    await attempt(
        "signup",
        "INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)",
        id=written.newcomer,
        org=graph.org,
        email=f"prev-new-{secrets.token_hex(6)}@alkera.dev",
    )
    # ensure_account: no target, so a lost race answers with no row.
    written.seat = await attempt(
        "seat",
        "INSERT INTO billing_accounts (id, scope, owner_user_id) "
        "VALUES (gen_random_uuid(), 'user', :owner) ON CONFLICT DO NOTHING RETURNING id",
        owner=written.newcomer,
    )
    await attempt(
        "team membership",
        "INSERT INTO team_memberships (id, user_id, team_id) VALUES (gen_random_uuid(), :u, :team)",
        u=written.newcomer,
        team=graph.sub,
    )
    await attempt(
        "role change",
        "UPDATE team_memberships SET role = 'admin' WHERE id = :id",
        id=graph.team_membership,
    )
    await attempt(
        "connection",
        "INSERT INTO team_connections (id, team_id, plugin, handle) "
        "VALUES (:id, :team, 'snowflake', :handle)",
        id=written.connection,
        team=graph.sub,
        handle=f"h-{written.connection.hex[:8]}",
    )
    await attempt(
        "grant on an existing connection",
        "INSERT INTO user_oauth_tokens (id, user_id, team_connection_id, "
        "access_token_encrypted) VALUES (:id, :u, :c, 'x')",
        id=written.grant,
        u=written.newcomer,
        c=graph.connection,
    )
    await attempt(
        "token refresh",
        "UPDATE user_oauth_tokens SET access_token_encrypted = 'refreshed' WHERE id = :id",
        id=graph.grant,
    )
    await attempt(
        "payload page",
        "INSERT INTO object_payload_rows (object_id, page, sha256, size) VALUES (:o, 1, :sha, 0)",
        o=graph.obj,
        sha="1" * 64,
    )
    for plugin, status in (("postgres", "verified"), ("bigquery", "unverifiable")):
        await attempt(
            f"inventory report ({plugin})",
            "INSERT INTO connection_inventory (id, user_id, workspace_key, plugin, status, "
            "last_verified_at) VALUES (gen_random_uuid(), :u, 'ws-1', :plugin, :status, now()) "
            f"ON CONFLICT ON CONSTRAINT {_INVENTORY_KEY} DO UPDATE SET "
            "status = excluded.status, last_verified_at = excluded.last_verified_at, "
            "reported_at = now()",
            u=graph.person,
            plugin=plugin,
            status=status,
        )
    return written, refused


async def _assert_every_row_landed(
    scratch: ScratchDatabase, graph: _Graph, written: _Written
) -> None:
    async with scratch.session() as session:
        assert written.seat is not None
        assert (
            await _scalar(
                session, "SELECT org_team_id FROM billing_accounts WHERE id = :i", i=written.seat
            )
            == graph.org
        )
        assert (
            await _scalar(
                session,
                "SELECT org_team_id FROM user_oauth_tokens WHERE id = :i",
                i=written.grant,
            )
            == graph.org
        )
        assert (
            await _scalar(
                session,
                "SELECT access_token_encrypted FROM user_oauth_tokens WHERE id = :i",
                i=graph.grant,
            )
            == "refreshed"
        )
        assert (
            await _scalar(
                session,
                "SELECT org_team_id FROM team_connections WHERE id = :i",
                i=written.connection,
            )
            == graph.org
        )
        assert (
            await _scalar(
                session,
                "SELECT count(*) FROM team_memberships WHERE user_id = :u AND org_team_id = :o",
                u=written.newcomer,
                o=graph.org,
            )
            == 1
        )
        assert (
            await _scalar(
                session, "SELECT role FROM team_memberships WHERE id = :i", i=graph.team_membership
            )
            == "admin"
        )
        inventory = {
            row.plugin: (row.status, row.org_team_id)
            for row in await session.execute(
                text(
                    "SELECT plugin, status, org_team_id FROM connection_inventory "
                    "WHERE user_id = :u AND workspace_key = 'ws-1'"
                ),
                {"u": graph.person},
            )
        }
        assert inventory == {
            "postgres": ("verified", graph.org),
            "bigquery": ("unverifiable", graph.org),
        }
        assert (
            await _scalar(
                session,
                "SELECT count(*) FROM object_payload_rows WHERE object_id = :o "
                "AND org_team_id = :org",
                o=graph.obj,
                org=graph.org,
            )
            == 1
        )


#: Where the previous release meets each schema of the roll: at head, and part
#: way through each revision that names an org on existing rows.
_STATES = (
    pytest.param(None, id="head"),
    pytest.param("0185", id="part-way-0185"),
    pytest.param("0187", id="part-way-0187"),
    pytest.param("0188", id="part-way-0188"),
    pytest.param("0189", id="part-way-0189"),
)


@pytest.mark.parametrize("stopped_in", _STATES)
async def test_the_previous_release_writes_through_the_roll(
    stopped_in: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PREVIOUS)
        graph = await _seed_previous(scratch)
        if stopped_in is None:
            await scratch.upgrade()
        else:
            await scratch.upgrade(f"{int(stopped_in) - 1:04d}")
            with monkeypatch.context() as patch:
                patch.setattr(
                    migration_safety, "outside_transaction", _stops_on_leaving_the_transaction
                )
                with pytest.raises(_RunDiedError):
                    await scratch.upgrade(stopped_in)

        written, refused = await _previous_release_writes(scratch, graph)

        assert refused == {}
        await scratch.upgrade()
        await _assert_every_row_landed(scratch, graph, written)


async def test_a_pool_still_refuses_an_org_of_its_own() -> None:
    """The seat fill reaches seats only: a pool written with an org breaks the
    rule that only a seat names one, as it always did."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            org = await _scalar(
                session,
                "INSERT INTO teams (id, name, is_root) VALUES (gen_random_uuid(), :n, true) "
                "RETURNING id",
                n=f"pool-{secrets.token_hex(4)}",
            )
            await session.commit()
            with pytest.raises(DBAPIError, match="ck_billing_accounts_user_scope_org"):
                await session.execute(
                    text(
                        "INSERT INTO billing_accounts (id, scope, owner_team_id, org_team_id) "
                        "VALUES (gen_random_uuid(), 'org', :o, :o)"
                    ),
                    {"o": org},
                )
            await session.rollback()
            pool = await _scalar(
                session,
                "INSERT INTO billing_accounts (id, scope, owner_team_id) "
                "VALUES (gen_random_uuid(), 'org', :o) RETURNING org_team_id",
                o=org,
            )
            assert pool is None
