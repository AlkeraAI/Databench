"""Shared stand-ins for the SQL broker's tests: a kernel context the broker can
ask about runs, and actors of each kind."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import duckdb
from alkera_notebook.sql.broker import RunInfo
from alkera_notebook.sql.provider import SqlWorkspace

ARROW_CODECS = frozenset({"json", "rows.json", "arrow.ipc.stream", "arrow.ipc.file"})


@dataclass(frozen=True)
class Who:
    kind: Literal["person", "agent", "system"]
    id: str


ALICE = Who("person", "alice")
AGENT = Who("agent", "agent-1")
SYSTEM = Who("system", "scheduler")


@dataclass
class Kernel:
    """The kernel's side as the broker sees it: which runs execute now."""

    data_dir: Path | None
    kernel_id: str = "k1"
    workspace: SqlWorkspace = field(default_factory=lambda: SqlWorkspace(id="ws1", root="/ws"))
    codecs: frozenset[str] = ARROW_CODECS
    sql_row_limit: int | None = None
    runs: dict[str, RunInfo] = field(default_factory=dict)

    def start(self, run_id: str, actor: Who = ALICE) -> None:
        self.runs[run_id] = RunInfo(run_id=run_id, actor=actor, notebook="n.alknb.py")

    def finish(self, run_id: str) -> None:
        self.runs.pop(run_id, None)

    def active_run(self, run_id: str) -> RunInfo | None:
        return self.runs.get(run_id)


def make_db(path: Path, rows: int = 100) -> Path:
    conn = duckdb.connect(str(path))
    conn.execute(
        "create table orders as select i as id, i * 1.5 as amount, 'c' || (i % 7) as customer "
        f"from range({rows}) t(i)"
    )
    conn.close()
    return path


ALKERA_CODECS_ALL = ARROW_CODECS


def file_bytes(path: Path) -> bytes:
    """A file's bytes, read synchronously (tests read result files in place)."""
    return path.read_bytes()
