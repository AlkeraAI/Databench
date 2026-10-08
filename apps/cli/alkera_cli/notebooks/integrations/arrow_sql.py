"""SQL results as Arrow for notebooks.

``RunSQLArrowCapability.run_arrow`` returns the whole result as an Arrow
table. For a PEP-249 connector the statement runs through the connector's
own gated path (``DbapiConnector.execute_native``: classification, cap
tokens, the read-only preamble and rollback, the statement timeout) and only
the final fetch differs: a driver that hands out Arrow itself (Snowflake's
``fetch_arrow_all``, Databricks' ``fetchall_arrow``, DuckDB and ADBC's
``fetch_arrow_table``) is asked for it, registered per engine. Every other
connector is adapted from its rows through ``to_arrow_table``.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

import pyarrow as pa

from alkera_cli.contracts.tool_types import CapToken, Effect, QueryResult
from alkera_cli.plugins.plugin_base.blob_compute import to_arrow_table
from alkera_cli.plugins.plugin_base.capabilities import RunSQLCapability
from alkera_cli.plugins.plugin_base.sql.connector import DbapiConnector, DbapiRunSQL

ArrowFetch = Callable[[Any], pa.Table | None]


@dataclasses.dataclass
class ArrowRun:
    """One statement's result: the Arrow table, and the connector's receipt
    (query id, engine, timing) the cost and audit paths read."""

    table: pa.Table
    receipt: QueryResult

    def reader(self) -> pa.RecordBatchReader:
        return pa.RecordBatchReader.from_batches(self.table.schema, self.table.to_batches())


class RunSQLArrowCapability(ABC):
    """Runs one statement and returns the whole result as Arrow."""

    @abstractmethod
    async def run_arrow(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        params: Mapping[str, Any] | None = None,
    ) -> ArrowRun: ...


class RowsArrowCapability(RunSQLArrowCapability):
    """Any ``RunSQLCapability``, adapted from its rows."""

    def __init__(self, inner: RunSQLCapability) -> None:
        self._inner = inner

    async def run_arrow(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        params: Mapping[str, Any] | None = None,
    ) -> ArrowRun:
        extra: dict[str, Any] = {"params": params} if params else {}
        result = await self._inner.run(sql, effect=effect, cap_token=cap_token, limit=None, **extra)
        table = to_arrow_table(result.columns, result.rows) if result.columns else pa.table({})
        return ArrowRun(table, result)


# ---------------------------------------------------------------- native fetches


def _snowflake(cursor: Any) -> pa.Table | None:
    fetch = getattr(cursor, "fetch_arrow_all", None)
    if not callable(fetch):
        return None
    table = fetch()
    # An empty result is None from the Snowflake connector.
    return table if table is not None else _empty(cursor)


def _databricks(cursor: Any) -> pa.Table | None:
    fetch = getattr(cursor, "fetchall_arrow", None)
    return fetch() if callable(fetch) else None


def _arrow_table(cursor: Any) -> pa.Table | None:
    fetch = getattr(cursor, "fetch_arrow_table", None)
    if not callable(fetch):
        return None
    table = fetch()
    if isinstance(table, pa.RecordBatchReader):
        return table.read_all()
    return table


def _empty(cursor: Any) -> pa.Table:
    names = [d[0] for d in cursor.description or []]
    return pa.table({n: pa.array([], pa.null()) for n in names})


#: Engines (``SqlEngineSpec.system``) whose driver returns Arrow itself. Read
#: only: every kernel a box runs shares it.
NATIVE_FETCHES: Final[Mapping[str, ArrowFetch]] = MappingProxyType(
    {
        "snowflake": _snowflake,
        "databricks": _databricks,
        "duckdb": _arrow_table,
        "postgres_adbc": _arrow_table,
    }
)


class DbapiArrowCapability(RunSQLArrowCapability):
    """A PEP-249 connector's gated path with the driver's own Arrow fetch."""

    def __init__(self, connector: DbapiConnector, fetch: ArrowFetch) -> None:
        self._connector = connector
        self._fetch = fetch

    async def run_arrow(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        params: Mapping[str, Any] | None = None,
    ) -> ArrowRun:
        result = await self._connector.execute_native(
            sql, effect=effect, cap_token=cap_token, params=params, fetch=self._fetch
        )
        receipt = result.receipt
        if isinstance(result.native, pa.Table):
            return ArrowRun(result.native, receipt)
        # No result set (a write), or the driver had no Arrow after all.
        table = to_arrow_table(receipt.columns, receipt.rows) if receipt.columns else pa.table({})
        return ArrowRun(table, receipt)


def arrow_capability(cap: RunSQLCapability) -> RunSQLArrowCapability:
    """The Arrow path for a connection's SQL capability."""
    if isinstance(cap, RunSQLArrowCapability):
        return cap
    if isinstance(cap, DbapiRunSQL):
        connector = cap.connector
        fetch = NATIVE_FETCHES.get(connector.spec.system)
        if fetch is not None:
            return DbapiArrowCapability(connector, fetch)
    return RowsArrowCapability(cap)
