"""Named connections mapped to DuckDB database files: the provider tests and
local demos use. A configurable delay before each statement makes
cancellation observable, and a configurable classifier lets a test pin how
statements are judged."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from alkera_notebook.sql.duck import DuckQuery, connect_hardened
from alkera_notebook.sql.errors import QueryFailedError
from alkera_notebook.sql.policy import StatementKind, keyword_classifier
from alkera_notebook.sql.provider import ArrowResult, Requester, SqlRequest, SqlWorkspace


@dataclass(frozen=True)
class FakeConnection:
    database: Path
    read_only: bool = True


@dataclass
class FakeConnectionProvider:
    connections: Mapping[str, FakeConnection | Path | str]
    delay_s: float = 0.0
    classifier: Callable[[str], StatementKind] = keyword_classifier
    name: str = "fake"
    executed: list[tuple[str, str, str]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    _queries: dict[str, DuckQuery] = field(default_factory=dict)

    def _connection(self, connection_name: str) -> FakeConnection | None:
        raw = self.connections.get(connection_name)
        if raw is None:
            return None
        return raw if isinstance(raw, FakeConnection) else FakeConnection(Path(raw))

    def can_resolve(self, connection_name: str, workspace: SqlWorkspace) -> bool:
        return self._connection(connection_name) is not None

    async def execute(
        self, request: SqlRequest, actor: Requester, workspace: SqlWorkspace
    ) -> ArrowResult:
        conn = self._connection(request.connection)
        assert conn is not None
        writes = self.classifier(request.sql) != "read"
        query = DuckQuery(connect_hardened(conn.database, read_only=conn.read_only and not writes))
        self._queries[query.query_id] = query
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            # Attribution only: who asked, for which run.
            self.executed.append((request.connection, actor.id, request.run_id))
            reader = await query.run(request.sql, request.params)
        except asyncio.CancelledError:
            self.cancelled.append(query.query_id)
            self._forget(query.query_id)
            raise
        except Exception as exc:
            self._forget(query.query_id)
            raise QueryFailedError(str(exc)) from exc
        return ArrowResult(
            reader=reader, query_id=query.query_id, release=lambda: self._forget(query.query_id)
        )

    async def cancel(self, query_id: str) -> None:
        query = self._queries.get(query_id)
        if query is not None:
            self.cancelled.append(query_id)
            query.interrupt()

    def _forget(self, query_id: str) -> None:
        query = self._queries.pop(query_id, None)
        if query is not None:
            query.close()
