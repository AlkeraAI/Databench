"""One bound for every DuckDB engine the tools open for model-authored SQL.

DuckDB reads any file the process can and calls it a table — ``read_text``,
``read_blob``, ``glob``, a bare ``'/path/x.csv'`` in a ``FROM`` — and the
permission classifier calls each of those a READ, which auto-allows with no card
in any stance and never meets the shell fence. A deny list of function names
cannot hold that line (a bare path has no function name), so the bound is the
engine's own and it is applied here, once, the same way for every connection:

- ``allowed_paths`` names the database file and its write-ahead log, and nothing
  for an in-memory engine. DuckDB admits the attached file whatever this list
  says, so the list is the contract stated rather than a check a test can
  remove; what a test CAN pin is that nothing beside the file is admitted;
- ``allowed_directories`` is empty. There is no directory allowance: nothing in
  the product needs a file beside the database (dbt loads its seeds into the
  file), and the ordinary layout — a ``.duckdb`` at the workspace root — puts
  ``.alkera/`` and ``.env`` beside it. Should a documented need appear, an
  allowance is a registration on top of a proper allowlist (every ``.alkera``
  found by walking, ``ALKERA_HOME`` and the home directory compared by file
  identity, the shell gate's secret-path list, resolved absolute paths), not a
  parameter here;
- ``enable_external_access`` is off: no other file, no network, no extension,
  no environment;
- ``lock_configuration`` is on, so no later statement — ``SET``, ``RESET``,
  ``PRAGMA`` — changes any of it, the resource ceilings included. The lock
  freezes whatever the settings were at that moment: a caller that wants a
  memory ceiling below DuckDB's default sets it BEFORE calling this.

The one DuckDB the tools open UNBOUNDED is
the DuckDB plugin's SDK client connection, the
``call_integration_sdk`` escape hatch: every run of it is human-approved with
the code on the card, and a fenced (cloud) session withholds the tool. That
exemption and this bound are one decision — change both or neither.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

#: The in-memory database, which has no file and so reaches no file.
MEMORY = ":memory:"


def database_paths(path: str) -> tuple[Path, ...]:
    """The files a DuckDB database at ``path`` needs: the file itself and its
    write-ahead log. The spill directory DuckDB adds on its own. Absolute but
    not resolved: DuckDB compares real paths itself, and the log sits beside the
    name the database was opened under. Nothing for an in-memory engine."""
    if path == MEMORY:
        return ()
    db = Path(path).absolute()
    return (db, db.with_name(db.name + ".wal"))


def confine_duckdb(con: duckdb.DuckDBPyConnection, *, paths: Sequence[Path] = ()) -> None:
    """Bound ``con`` to ``paths`` and lock it there. Call it first thing after
    ``duckdb.connect``: the lock refuses every later change."""
    con.execute("SET allowed_paths = ?", [[str(p) for p in paths]])
    con.execute("SET allowed_directories = []")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")


__all__ = ["MEMORY", "confine_duckdb", "database_paths"]
