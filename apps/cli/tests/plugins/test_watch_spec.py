"""Per-connection file ownership for the lineage/KB file watcher.

`WatchSpec` maps a connection to the files + dirs it owns; an edit / add / remove to one
triggers that connection's refresh. These pin: each plugin's watch spec (dbt = manifest-only
file, DuckDB/Tableau = file, Airflow = dir + ``*.py``, warehouse = None), that file-roots
match EXACTLY while dir-roots match by pattern (so an ADDED or REMOVED file under them counts,
not only edits), and the ONE-TO-MANY match (a shared file → every owning connection).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from alkera_cli.plugins.plugin_base.artifacts import (
    WatchSpec,
    artifact_fingerprint,
    connection_watch_spec,
    match_connections,
)


def _conn(handle: str, **attributes: str) -> SimpleNamespace:
    return SimpleNamespace(handle=handle, attributes=dict(attributes))


def test_dbt_watches_the_compiled_manifest_only(tmp_path: Path) -> None:
    # dbt is MANIFEST-ONLY: the spec is target/manifest.json (a file), not the project source —
    # editing a .sql doesn't change lineage until a recompile rewrites the manifest.
    manifest = tmp_path / "proj" / "target" / "manifest.json"
    conn = _conn("dbt_jaffle", manifest_path=str(manifest), project_dir=str(tmp_path / "proj"))
    spec = connection_watch_spec(conn)
    assert spec is not None
    assert spec.files == (manifest,) and spec.dirs == ()
    # The OS watcher is handed the manifest's PARENT dir (catches an atomic save-rename).
    assert spec.watch_dirs() == {manifest.parent}
    assert spec.matches(manifest)
    # A sibling source edit is NOT owned (manifest-only, despite project_dir being known).
    assert not spec.matches(tmp_path / "proj" / "models" / "orders.sql")


def test_duckdb_and_tableau_watch_their_single_file(tmp_path: Path) -> None:
    for conn, f in (
        (_conn("dd", path=str(tmp_path / "w.duckdb")), tmp_path / "w.duckdb"),
        (_conn("tb", workbook_path=str(tmp_path / "book.twbx")), tmp_path / "book.twbx"),
    ):
        spec = connection_watch_spec(conn)
        assert spec is not None and spec.files == (f,) and spec.dirs == ()
        assert spec.matches(f) and spec.watch_dirs() == {f.parent}


def test_airflow_watches_the_dags_dir_for_added_and_removed_py(tmp_path: Path) -> None:
    dags = tmp_path / "dags"
    spec = connection_watch_spec(_conn("af", dags_path=str(dags)))
    assert spec is not None and spec.dirs == (dags,) and spec.patterns == ("*.py", "*.sql")
    assert spec.watch_dirs() == {dags}
    # A NEW or REMOVED .py anywhere under the dir is owned — matched by PATH, so whether the
    # file currently exists is irrelevant (a deletion still triggers the refresh).
    assert spec.matches(dags / "new_dag.py")
    assert spec.matches(dags / "sub" / "nested_dag.py")  # recursive
    assert spec.matches(dags / "queries" / "load.sql")  # an external operator .sql is watched
    assert not spec.matches(dags / "README.md")  # non-*.py/.sql ignored
    assert not spec.matches(tmp_path / "other" / "x.py")  # outside the dir


def test_dags_dir_fingerprint_changes_on_an_in_place_edit(tmp_path: Path) -> None:
    # The dags/ fingerprint must be DIRECTORY-AWARE: an in-place rewrite of a contained .py or
    # .sql must change it (the directory's own mtime only moves on add/remove, so a bare dir
    # stat would skip the re-seed and leave lineage + the dag_card stale).
    import os

    dags = tmp_path / "dags"
    (dags / "sql").mkdir(parents=True)
    (dags / "etl.py").write_text("SELECT 1")
    (dags / "sql" / "load.sql").write_text("SELECT a FROM t")
    conn = _conn("af", dags_path=str(dags))
    fp0 = artifact_fingerprint(conn)
    # in-place rewrite of the .py (new content, bumped mtime — the dir entry is untouched)
    (dags / "etl.py").write_text("SELECT 1, 2")
    os.utime(dags / "etl.py", (1, 1))
    fp_py = artifact_fingerprint(conn)
    assert fp_py != fp0
    # in-place rewrite of the contained .sql also registers
    (dags / "sql" / "load.sql").write_text("SELECT a, b FROM t JOIN u")
    os.utime(dags / "sql" / "load.sql", (2, 2))
    assert artifact_fingerprint(conn) != fp_py
    # a missing dir → None (can't tell → always re-emit), never a stale token
    assert artifact_fingerprint(_conn("gone", dags_path=str(tmp_path / "nope"))) is None


def test_live_warehouse_has_no_watch_spec() -> None:
    # Snowflake/BigQuery have no offline artifact → nothing to watch.
    assert connection_watch_spec(_conn("snow", account="a", warehouse="w")) is None


def test_dir_root_without_patterns_matches_any_file(tmp_path: Path) -> None:
    spec = WatchSpec(dirs=(tmp_path,))  # empty patterns ⇒ every file under the dir is owned
    assert spec.matches(tmp_path / "anything.xyz")
    assert spec.matches(tmp_path / "deep" / "nested.bin")


def test_match_connections_is_one_to_many(tmp_path: Path) -> None:
    shared = tmp_path / "shared.duckdb"
    a = _conn("a", path=str(shared))
    b = _conn("b", path=str(shared))  # two connections backed by the SAME file
    c = _conn("c", path=str(tmp_path / "other.duckdb"))
    warehouse = _conn("w", account="acct")  # no artifact
    matched = match_connections(shared, [a, b, c, warehouse])
    assert {m.handle for m in matched} == {"a", "b"}  # both owners; not c / warehouse
    assert match_connections(tmp_path / "nope.duckdb", [a, b, c]) == []


def test_matches_normalizes_dotted_paths(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "w.duckdb"
    spec = WatchSpec(files=(f,))
    assert spec.matches(tmp_path / "sub" / ".." / "sub" / "w.duckdb")  # normalized equal
