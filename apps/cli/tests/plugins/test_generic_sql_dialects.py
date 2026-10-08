"""The generic SQL connector on SQLite, DuckDB and Postgres, end to end.

A connection added from a URL is queried through the agent's ``sql.query`` and
browsed through ``sql.schema``, on the open generic plugin. A read opens read-only
where the backend has a way, so the engine itself refuses a write that reached it,
whatever the classifier said; the statement classifier and the always-rollback
stay the second line.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import psycopg
import pytest
from alkera_cli.contracts.tool_types import CredentialRef
from alkera_cli.plugins.generic_sql.plugin import (
    DIALECTS,
    UNKNOWN_DIALECT,
    GenericSqlCapabilities,
    GenericSqlPlugin,
    spec_for,
)
from alkera_cli.plugins.plugin_base import Connection, ToolRegistry
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_core.project.directory import ProjectDirectory
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.asyncio


def _sqlite(path: Path) -> str:
    con = sqlite3.connect(path)
    con.execute("create table orders (id integer, amount integer)")
    con.executemany("insert into orders values (?, ?)", [(i, i * 2) for i in range(1, 6)])
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def _duckdb(path: Path) -> str:
    con = duckdb.connect(str(path))
    con.execute("create table orders as select i as id, i * 2 as amount from range(1, 6) t(i)")
    con.close()
    return f"duckdb:///{path}"


def _connection(url: str, handle: str = "db") -> Connection:
    conn, _credential = GenericSqlPlugin().build_connection(handle, "", {"url": url})
    return conn


def _registry(tmp_path: Path, conn: Connection) -> tuple[ToolRegistry, ProjectDirectory]:
    project = ProjectDirectory(tmp_path / ".alkera")
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    registry.register_connection(conn, capabilities=GenericSqlCapabilities().capabilities(conn))
    register_sql_tools(registry)
    return registry, project


async def _query(tmp_path: Path, conn: Connection, sql: str) -> dict[str, Any]:
    registry, project = _registry(tmp_path, conn)
    return await registry.dispatch(
        "sql.query",
        {"mode": "sql", "connection": conn.handle, "sql": sql},
        decision_sink=DecisionSink(project.path),
        alkera_dir=project.path,
    )


@pytest.fixture(params=["sqlite", "duckdb"])
def file_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    path = tmp_path / f"warehouse.{request.param}"
    return _sqlite(path) if request.param == "sqlite" else _duckdb(path)


async def test_the_agent_reads_a_file_database(tmp_path: Path, file_url: str) -> None:
    out = await _query(tmp_path, _connection(file_url), "select sum(amount) as total from orders")
    assert out["preview_rows"] == [[30]]


async def test_the_agent_browses_the_schema(tmp_path: Path, file_url: str) -> None:
    registry, project = _registry(tmp_path, _connection(file_url))
    listed = await registry.dispatch(
        "sql.schema",
        {"mode": "list", "connection": "db"},
        decision_sink=DecisionSink(project.path),
        alkera_dir=project.path,
    )
    assert "orders" in str(listed)


async def test_an_unapproved_write_changes_nothing(tmp_path: Path, file_url: str) -> None:
    conn = _connection(file_url)
    refused = await _query(tmp_path, conn, "delete from orders")
    assert refused.get("_alkera_tool_error") is True
    count = await _query(tmp_path, conn, "select count(*) as n from orders")
    assert count["preview_rows"] == [[5]]


def _engine_write_on_a_read(conn: Connection, write: str) -> None:
    """A write handed to the engine inside a read, past the classifier: the read's
    connection and preamble are the only thing left to refuse it."""
    spec = spec_for(conn)
    raw = spec.connect(conn, read_only=True)
    try:
        cur = raw.cursor()
        for stmt in spec.read_only_preamble:
            cur.execute(stmt)
        if spec.read_only_snapshot_stmt:
            cur.execute(spec.read_only_snapshot_stmt)
        cur.execute(write)
    finally:
        # A refused statement can leave no transaction to roll back (DuckDB).
        with contextlib.suppress(Exception):
            raw.rollback()
        raw.close()


async def test_the_engine_refuses_a_write_on_a_read(tmp_path: Path, file_url: str) -> None:
    with pytest.raises(Exception, match=r"(?i)read.?only|readonly|query_only|attempt to write"):
        _engine_write_on_a_read(_connection(file_url), "delete from orders")


def test_a_known_dialect_reads_read_only_and_an_unknown_one_relies_on_the_gate() -> None:
    assert DIALECTS["sqlite"].read_only_preamble == ("PRAGMA query_only = ON",)
    assert DIALECTS["postgres"].read_only_preamble == ("SET TRANSACTION READ ONLY",)
    conn = _connection("mssql+pyodbc://u@db.example.com/x")
    assert spec_for(conn).read_only_preamble == UNKNOWN_DIALECT.read_only_preamble == ()


# -- Postgres, the suite's own test database ----------------------------------------------

PG_TABLE = "generic_sql_dialect_orders"


@pytest.fixture
def pg_url() -> Any:
    url = make_url(os.environ["DATABASE_URL_SYNC"]).set(drivername="postgresql+psycopg")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"drop table if exists {PG_TABLE}")
        con.execute(f"create table {PG_TABLE} (id int, amount int)")
        con.execute(f"insert into {PG_TABLE} select i, i * 2 from generate_series(1, 5) i")
    yield url.render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f"drop table if exists {PG_TABLE}")


def _pg_connection(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Connection:
    """The URL's password held apart, the way the form stores it, and resolved from
    the environment at connect."""
    conn = _connection(pg_url)
    monkeypatch.setenv("GENERIC_SQL_TEST_PASSWORD", make_url(pg_url).password or "")
    return conn.model_copy(
        update={"credential_ref": CredentialRef(scheme="env", locator="GENERIC_SQL_TEST_PASSWORD")}
    )


async def test_the_agent_reads_postgres(
    tmp_path: Path, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _pg_connection(pg_url, monkeypatch)
    out = await _query(tmp_path, conn, f"select sum(amount) from {PG_TABLE}")
    assert out["preview_rows"] == [[30]]


async def test_postgres_refuses_a_write_on_a_read(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _pg_connection(pg_url, monkeypatch)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        _engine_write_on_a_read(conn, f"delete from {PG_TABLE}")
