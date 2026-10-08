"""DuckDB on the engine side: external access off, no extension autoload or
autoinstall, configuration locked, so a statement can read the database it
was given and nothing else on the engine's machine."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import duckdb
    import pyarrow as pa

T = TypeVar("T")

HARDENED: dict[str, Any] = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
    "lock_configuration": True,
}


def connect_hardened(
    database: str | Path, *, read_only: bool = True, threads: int = 2, memory_limit: str = "1GB"
) -> duckdb.DuckDBPyConnection:
    import duckdb

    config = {**HARDENED, "threads": threads, "memory_limit": memory_limit}
    # lock_configuration must come last, after the settings it freezes.
    ordered = {k: v for k, v in config.items() if k != "lock_configuration"}
    ordered["lock_configuration"] = True
    return duckdb.connect(str(database), read_only=read_only, config=ordered)


class DuckQuery:
    """One statement on a hardened connection, run in a worker thread and
    interruptible from the event loop. Closing waits for the worker: a
    connection is never closed under a statement still running on it."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.query_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._busy = 0
        self._close_requested = False
        self._closed = False

    def _enter(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("the query was cancelled")
            self._busy += 1

    def _exit(self) -> None:
        with self._lock:
            self._busy -= 1
            close_now = self._close_requested and self._busy == 0 and not self._closed
            if close_now:
                self._closed = True
        if close_now:
            with contextlib.suppress(Exception):
                self.conn.close()

    def call(self, fn: Callable[[], T]) -> T:
        """Runs ``fn`` (in the caller's thread) while holding the connection open."""
        self._enter()
        try:
            return fn()
        finally:
            self._exit()

    async def run(
        self, sql: str, params: Any = None, batch_rows: int = 65_536
    ) -> pa.RecordBatchReader:
        def work() -> pa.RecordBatchReader:
            cursor = self.conn.execute(sql, params) if params else self.conn.execute(sql)
            to_reader = getattr(cursor, "to_arrow_reader", None)
            if to_reader is not None:
                return to_reader(batch_rows)
            return cursor.fetch_record_batch(batch_rows)

        future = asyncio.ensure_future(asyncio.to_thread(self.call, work))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.interrupt()
            raise

    def interrupt(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.interrupt()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._close_requested = True
            close_now = self._busy == 0
            if close_now:
                self._closed = True
        if close_now:
            with contextlib.suppress(Exception):
                self.conn.close()
