"""The generic DBAPI connector — the capability-gated chokepoint for any PEP-249 SQL
engine, parameterized by a :class:`SqlEngineSpec`.

It RE-CLASSIFIES the SQL itself (never trusting the caller's claimed effect) and
REFUSES any write/destroy/egress without a valid, unexpired ``CapToken`` bound to THIS
action — the unbypassable floor that catches even a dynamically-built ``DELETE``. Reads
run inside a transaction that is ALWAYS rolled back (plus an engine read-only preamble),
so a misclassified mutation can't commit. The sync driver is wrapped in
``asyncio.to_thread`` so it never blocks the event loop; the secret is resolved inside
``spec.connect`` at the I/O boundary and never logged or returned into agent context.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from alkera_cli.contracts.tool_types import (
    CapToken,
    Effect,
    QueryResult,
)
from alkera_cli.plugins.plugin_base.capabilities import RunSQLCapability
from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.connector import Connector
from alkera_cli.plugins.plugin_base.permissions import descriptor_from_sql
from alkera_cli.plugins.plugin_base.sql.spec import SqlEngineSpec
from alkera_cli.plugins.plugin_base.sql.timeout import resolve_sql_statement_timeout_seconds

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.capabilities import IntrospectSchemaCapability


#: Takes a statement's whole result from the driver's cursor in the driver's
#: own form (an Arrow table), or ``None`` when it cannot.
ResultFetch = Callable[[Any], Any]


@dataclass(frozen=True)
class NativeResult:
    """The connector's receipt (query id, engine, timing, row count) and the
    result as ``fetch`` returned it (``None``: the receipt carries the rows)."""

    receipt: QueryResult
    native: Any


def _native_rows(native: Any) -> int:
    rows = getattr(native, "num_rows", None)
    return int(rows) if isinstance(rows, int) else 0


class SqlCapabilityDeniedError(Exception):
    """A write/destroy/egress reached the connector without a valid cap-token —
    the unbypassable floor refused it."""


class DbapiConnector(Connector):
    """The capability-gated path to a PEP-249 SQL engine, driven by a ``SqlEngineSpec``."""

    def __init__(
        self,
        connection: Connection,
        spec: SqlEngineSpec,
        *,
        timeout_resolver: Callable[[], int] = resolve_sql_statement_timeout_seconds,
    ) -> None:
        self.connection = connection
        self.spec = spec
        # Resolves the server-side statement timeout (seconds) at run time — injectable
        # so a test pins it without touching the real preferences file. Only CALLED for
        # specs that declare a timeout statement (the resolve reads the prefs file).
        self._timeout_resolver = timeout_resolver

    async def execute(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        receipt, _native = await self._execute(
            sql, effect=effect, cap_token=cap_token, limit=limit, params=params, fetch=None
        )
        return receipt

    async def execute_native(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        fetch: ResultFetch,
        params: Mapping[str, Any] | None = None,
    ) -> NativeResult:
        """Run ``sql`` through the same gated path as :meth:`execute` (the
        classification, the cap token, the read-only preamble and rollback, the
        statement timeout), but take the whole result with ``fetch(cursor)``:
        a driver's own columnar fetch (Arrow) instead of Python rows. When
        ``fetch`` returns ``None`` (the driver cannot), the rows are fetched as
        usual and ``native`` is ``None``. The receipt's ``rows`` stay empty when
        ``native`` holds the result."""
        receipt, native = await self._execute(
            sql, effect=effect, cap_token=cap_token, limit=None, params=params, fetch=fetch
        )
        return NativeResult(receipt=receipt, native=native)

    async def _execute(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: Mapping[str, Any] | None,
        fetch: ResultFetch | None,
    ) -> tuple[QueryResult, Any]:
        # Re-classify here — the connector NEVER trusts the caller's `effect`. The
        # text classified is the text executed: with bound parameters that is the
        # placeholder-bearing statement, so a value can never widen what was judged.
        descriptor = descriptor_from_sql(
            sql,
            dialect=self.spec.classifier_dialect,
            capability="sql",
            connection=self.connection.handle,
        )
        # A READ runs with no cap token in a transaction the read-only preamble
        # constrains and the rollback undoes — but that containment is a PER-
        # STATEMENT guarantee. A multi-statement string sent whole over the
        # simple-query protocol can carry a `SET TRANSACTION READ WRITE` or a
        # `COMMIT` that dismantles it and smuggle a write behind statements the
        # classifier read as harmless. So a READ path accepts EXACTLY ONE
        # statement; a chain is never a read and needs an approved cap token.
        if descriptor.effect == Effect.READ and len(descriptor.statements) > 1:
            raise SqlCapabilityDeniedError(
                f"multiple statements on {self.connection.handle!r} refused: a read "
                "runs one statement at a time (a statement chain is never a read). "
                "Send one query."
            )
        if descriptor.effect != Effect.READ and (
            cap_token is None or not cap_token.authorizes(descriptor)
        ):
            raise SqlCapabilityDeniedError(
                f"{descriptor.operation or descriptor.effect.value} on "
                f"{self.connection.handle!r} refused: no valid capability token "
                "(write/destroy/egress requires broker approval)"
            )
        # Resolve the timeout only for engines that can apply it (the resolve reads the
        # prefs file / env); local + stateless engines declare no statement and skip it.
        timeout_seconds = self._timeout_resolver() if self.spec.statement_timeout_stmt else 0
        bound = dict(params) if params else None
        return await asyncio.to_thread(
            self._run_sync, sql, bound, descriptor.effect, limit, timeout_seconds, fetch
        )

    def _run_sync(
        self,
        sql: str,
        params: dict[str, Any] | None,
        effect: Effect,
        limit: int | None,
        timeout_seconds: int,
        fetch: ResultFetch | None = None,
    ) -> tuple[QueryResult, Any]:
        read_only = effect == Effect.READ
        native: Any = None
        # The receipt's clock: when the statement ran and how long the whole
        # round trip took (session open through the last row fetched), so a
        # number the reader is shown can be dated and weighed without a click.
        executed_at = datetime.now(UTC)
        clock_start = time.monotonic()
        conn = self.spec.connect(self.connection, read_only=read_only)
        try:
            cur = conn.cursor()
            try:
                # Cap the query SERVER-SIDE first (before the read-only preamble, since the
                # SET isn't a "query" so Postgres `SET TRANSACTION READ ONLY` after it still
                # holds) — applied to reads AND writes, so a runaway query of either kind is
                # cancelled by the engine rather than billing unbounded compute. `0` (the user
                # disabled it) or an engine that declares no statement skips this.
                if self.spec.statement_timeout_stmt and timeout_seconds > 0:
                    cur.execute(
                        self.spec.statement_timeout_stmt.format(
                            seconds=timeout_seconds, ms=timeout_seconds * 1000
                        )
                    )
                if read_only:
                    # Defense in depth: make the engine itself reject a misclassified
                    # write (Postgres `SET TRANSACTION READ ONLY`, etc.). Empty for
                    # engines that open a read-only *connection* instead (DuckDB).
                    for stmt in self.spec.read_only_preamble:
                        cur.execute(stmt)
                    # Take the transaction's snapshot BEFORE the user statement so the
                    # engine locks the read/write mode: on Postgres a `SET TRANSACTION
                    # READ WRITE` is only legal before the first query, so this one
                    # harmless query (`SELECT 1`) makes the engine itself refuse any
                    # later attempt to leave the read-only transaction. Empty for
                    # engines whose containment is a read-only connection, not a txn.
                    if self.spec.read_only_snapshot_stmt:
                        cur.execute(self.spec.read_only_snapshot_stmt)
                # The values ride BESIDE the statement (PEP-249's second argument):
                # the driver binds them on its side, so the SQL text never carries
                # one. Without values the statement goes alone — a driver reading a
                # literal `%` as a directive only does so when values are passed.
                if params is not None:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                # Only fetch when the statement produced a result set. A non-RETURNING
                # write (INSERT/UPDATE/DELETE) leaves cur.description None, and a strict
                # PEP-249 driver (psycopg3) RAISES on a fetch with no result — so an
                # approved write would otherwise blow up here and never commit.
                description = cur.description
                has_rows = description is not None
                columns = [d[0] for d in description] if description is not None else []
                if has_rows and fetch is not None:
                    native = fetch(cur)
                if not has_rows or native is not None:
                    rows, truncated = [], False
                elif limit is not None:
                    fetched = cur.fetchmany(limit + 1)  # +1 to detect truncation
                    truncated = len(fetched) > limit
                    rows = [list(r) for r in fetched[:limit]]
                else:
                    rows = [list(r) for r in cur.fetchall()]
                    truncated = False
                query_id = (
                    getattr(cur, self.spec.query_id_attr, None) if self.spec.query_id_attr else None
                )
                result = QueryResult(
                    columns=columns,
                    rows=rows,
                    row_count=len(rows) if native is None else _native_rows(native),
                    truncated=truncated,
                    query_id=query_id,
                    engine=self.spec.system,
                    executed_at=executed_at,
                    duration_ms=int((time.monotonic() - clock_start) * 1000),
                )
            finally:
                # A read is ALWAYS rolled back so any DML a misclassification let through
                # is undone; an approved write commits (below, only on success).
                if read_only:
                    with contextlib.suppress(Exception):
                        conn.rollback()
                with contextlib.suppress(Exception):
                    cur.close()
            if not read_only:
                # NOT suppressed: a failed commit (deferred constraint, serialization
                # failure, dropped connection) must surface — never report a rolled-back
                # write as success.
                conn.commit()
            return result, native
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    async def introspect(self) -> IntrospectSchemaCapability:
        from alkera_cli.plugins.plugin_base.sql.introspect import GenericSqlIntrospect

        return GenericSqlIntrospect(self)


class DbapiRunSQL(RunSQLCapability):
    """The ``RunSQLCapability`` the ``sql.query`` tool consumes — delegates to the
    capability-gated :class:`DbapiConnector`."""

    def __init__(self, connector: DbapiConnector) -> None:
        self._connector = connector

    @property
    def connector(self) -> DbapiConnector:
        """The gated connector, for callers that need its native fetch path."""
        return self._connector

    @property
    def read_only_reason(self) -> str:
        return self._connector.spec.read_only_reason

    async def run(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        return await self._connector.execute(
            sql, effect=effect, cap_token=cap_token, limit=limit, params=params
        )


__all__ = [
    "DbapiConnector",
    "DbapiRunSQL",
    "NativeResult",
    "ResultFetch",
    "SqlCapabilityDeniedError",
]
