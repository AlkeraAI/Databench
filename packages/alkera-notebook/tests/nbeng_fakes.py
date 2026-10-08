"""Test doubles for the engine's tests: the fake kernel's mount (run by the
real ``LocalSubprocessLauncher``), a fake SQL provider and a
clock tests move by hand."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alkera_notebook.kernels.launch_local import group_pids

FAKE_MOUNT = str(Path(__file__).resolve().parent / "nbeng_fake_kernel")


def group_alive(pgid: int) -> bool:
    return bool(group_pids(pgid))


def write_duck(path: Path, values: Sequence[int]) -> Path:
    """A DuckDB file holding table ``t(v)`` with ``values``."""
    import duckdb

    con = duckdb.connect(str(path))
    try:
        con.execute("create or replace table t (v integer)")
        if values:
            con.executemany("insert into t values (?)", [[v] for v in values])
    finally:
        con.close()
    return path


def duck_provider(directory: Path, values: Sequence[int] = (1, 2, 3), **extra: Any) -> Any:
    """The fake SQL provider with one workspace connection,
    ``Warehouse``, over a DuckDB file; ``extra`` adds more connections."""
    from alkera_notebook.sql.providers.fake import FakeConnection, FakeConnectionProvider

    directory.mkdir(parents=True, exist_ok=True)
    db = write_duck(directory / "warehouse.duckdb", values)
    return FakeConnectionProvider({"Warehouse": FakeConnection(db), **extra})


class ManualClock:
    """A clock tests move by hand; ``sleep`` still yields to the loop."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.01))


PYTHON = sys.executable
