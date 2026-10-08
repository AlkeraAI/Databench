"""Connections for the standalone engine, from a file on the person's machine.

``~/.config/alkera/connections.toml`` (``$XDG_CONFIG_HOME`` honoured) names
each connection with either a SQLAlchemy URL or an ADBC driver::

    [connections.local]
    url = "duckdb:///Users/me/data/shop.duckdb"

    [connections.warehouse]
    url = "postgresql+psycopg://me@db.internal/analytics"

    [connections.pg_arrow]
    adbc_driver = "adbc_driver_postgresql"
    uri = "postgresql://me@db.internal/analytics"

Credentials stay in the engine; the kernel only ever sees results. The file
is read on each resolution, so an edit takes effect on the next statement.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alkera_notebook.sql.duck import DuckQuery, connect_hardened
from alkera_notebook.sql.errors import QueryFailedError, UnknownConnectionError
from alkera_notebook.sql.policy import StatementKind, keyword_classifier
from alkera_notebook.sql.provider import ArrowResult, Requester, SqlRequest, SqlWorkspace


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "alkera" / "connections.toml"


@dataclass(frozen=True)
class LocalConnection:
    name: str
    url: str = ""
    adbc_driver: str = ""
    uri: str = ""
    read_only: bool = True


def load_connections(path: Path) -> dict[str, LocalConnection]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        doc = tomllib.load(f)
    out: dict[str, LocalConnection] = {}
    for name, entry in (doc.get("connections") or {}).items():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "")
        driver = str(entry.get("adbc_driver") or "")
        if not url and not driver:
            continue
        out[name] = LocalConnection(
            name=name,
            url=url,
            adbc_driver=driver,
            uri=str(entry.get("uri") or ""),
            read_only=bool(entry.get("read_only", True)),
        )
    return out


@dataclass
class LocalConfigProvider:
    path: Path = field(default_factory=default_config_path)
    classifier: Callable[[str], StatementKind] = keyword_classifier
    name: str = "local-config"
    _cancels: dict[str, Callable[[], None]] = field(default_factory=dict)

    def _get(self, connection_name: str) -> LocalConnection | None:
        return load_connections(self.path).get(connection_name)

    def can_resolve(self, connection_name: str, workspace: SqlWorkspace) -> bool:
        return self._get(connection_name) is not None

    async def execute(
        self, request: SqlRequest, actor: Requester, workspace: SqlWorkspace
    ) -> ArrowResult:
        conn = self._get(request.connection)
        if conn is None:
            raise UnknownConnectionError(request.connection)
        writes = self.classifier(request.sql) != "read"
        if conn.adbc_driver:
            return await self._adbc(conn, request)
        if conn.url.startswith("duckdb:///"):
            return await self._duckdb(conn, request, read_only=conn.read_only and not writes)
        return await self._sqlalchemy(conn, request)

    async def cancel(self, query_id: str) -> None:
        cancel = self._cancels.pop(query_id, None)
        if cancel is not None:
            with contextlib.suppress(Exception):
                cancel()

    async def _duckdb(
        self, conn: LocalConnection, request: SqlRequest, *, read_only: bool
    ) -> ArrowResult:
        database = conn.url.removeprefix("duckdb:///")
        query = DuckQuery(connect_hardened(database, read_only=read_only))
        self._cancels[query.query_id] = query.interrupt
        try:
            reader = await query.run(request.sql, request.params)
        except asyncio.CancelledError:
            query.close()
            raise
        except Exception as exc:
            query.close()
            raise QueryFailedError(str(exc)) from exc

        def release() -> None:
            self._cancels.pop(query.query_id, None)
            query.close()

        return ArrowResult(reader=reader, query_id=query.query_id, release=release)

    async def _adbc(self, conn: LocalConnection, request: SqlRequest) -> ArrowResult:
        dbapi: Any = importlib.import_module(f"{conn.adbc_driver}.dbapi")
        query_id = uuid.uuid4().hex

        def run() -> tuple[Any, Any]:
            connection = dbapi.connect(conn.uri)
            cursor = connection.cursor()
            self._cancels[query_id] = cursor.adbc_cancel
            cursor.execute(request.sql, request.params)
            return connection, cursor

        try:
            connection, cursor = await asyncio.to_thread(run)
        except Exception as exc:
            self._cancels.pop(query_id, None)
            raise QueryFailedError(str(exc)) from exc

        def release() -> None:
            self._cancels.pop(query_id, None)
            with contextlib.suppress(Exception):
                cursor.close()
            with contextlib.suppress(Exception):
                connection.close()

        return ArrowResult(reader=cursor.fetch_record_batch(), query_id=query_id, release=release)

    async def _sqlalchemy(self, conn: LocalConnection, request: SqlRequest) -> ArrowResult:
        import pyarrow as pa
        import sqlalchemy as sa

        query_id = uuid.uuid4().hex

        def run() -> pa.Table:
            engine = sa.create_engine(conn.url)
            try:
                with engine.connect() as connection:
                    raw = connection.connection.dbapi_connection
                    cancel = getattr(raw, "cancel", None)
                    if callable(cancel):
                        self._cancels[query_id] = cancel
                    result = connection.execute(sa.text(request.sql), request.params or {})
                    if not result.returns_rows:
                        connection.commit()
                        return pa.table({"rows_affected": [result.rowcount]})
                    names = list(result.keys())
                    rows = [tuple(r) for r in result]
                    columns = {n: [r[i] for r in rows] for i, n in enumerate(names)}
                    return pa.table(columns)
            finally:
                engine.dispose()
                self._cancels.pop(query_id, None)

        try:
            table = await asyncio.to_thread(run)
        except Exception as exc:
            raise QueryFailedError(str(exc)) from exc
        return ArrowResult(
            reader=pa.RecordBatchReader.from_batches(table.schema, table.to_batches()),
            query_id=query_id,
        )
