"""Offline-artifact freshness + watch-ownership helpers, shared by the activation SEED,
the background REFRESH runner (so both gate identically on a connection's backing file),
and the file watcher (so an edit to a connection's file triggers its refresh).

A connection seeded from a local artifact (a dbt ``manifest.json``, a ``.duckdb``
file, a Tableau ``.twb``/``.twbx`` workbook, an Airflow ``dags/`` folder) is re-read
only when that artifact changed — so an unchanged project is a stat-only no-op. There
is NO size cap: a project of any size seeds (the heavy work is off the event loop and
the column re-parse is incremental + budgeted, so it can't freeze the daemon).
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

#: The attributes a connection may be seeded FROM. ``manifest_path`` + ``catalog_path`` (dbt)
#: and ``path`` (DuckDB) are files; ``workbook_path`` (Tableau) is a file; ``dags_path``
#: (Airflow) is a folder. A connection may carry SEVERAL (dbt has manifest.json AND
#: catalog.json) — the freshness mtime is the MAX across all present, and the watcher owns all
#: of them, so a `dbt docs generate` that rewrites ONLY catalog.json still re-seeds.
_ARTIFACT_ATTRS = ("manifest_path", "catalog_path", "path", "workbook_path", "dags_path")

#: Connection attributes that point at a DIRECTORY (every file under it, filtered by a
#: pattern, is owned) rather than a single artifact file.
_DIR_ARTIFACT_ATTRS = {"dags_path": ("*.py", "*.sql")}


def _dir_entries(root: Path, patterns: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """``(relpath, mtime_ns, size)`` for every owned file under a directory artifact, sorted +
    deduped. The basis for both the directory FINGERPRINT and its max-mtime — so an IN-PLACE
    edit of a contained file registers (a directory's own ``stat`` only moves on add/remove)."""
    entries: set[tuple[str, int, int]] = set()
    for pat in patterns:
        for f in root.rglob(pat):
            with contextlib.suppress(OSError):
                st = f.stat()
                entries.add((f.relative_to(root).as_posix(), st.st_mtime_ns, st.st_size))
    return sorted(entries)


#: Cadence for an OFFLINE file-backed connector's background refresh job. It's cheap
#: (the refresh runner mtime-gates it, so an unchanged artifact is a stat-only no-op),
#: so a frequent poll keeps the graph fresh against ``dbt parse`` / ``.duckdb`` /
#: workbook edits without meaningful cost.
OFFLINE_REFRESH_CADENCE_SECONDS = 900

#: Cadence for a LIVE warehouse's FREE structural refresh. A live connection has NO
#: artifact mtime to gate on (``source_fingerprint`` is None), so EVERY beat fires a real
#: ``INFORMATION_SCHEMA`` introspection query — unlike the offline path's stat-only no-op.
#: An hourly poll (matching Snowflake's structural cadence) keeps the schema graph fresh
#: without re-querying a live warehouse four times an hour. The BILLED sources stay on the
#: separate manual-only metered job regardless of this cadence.
LIVE_WAREHOUSE_REFRESH_CADENCE_SECONDS = 3600


def artifact_mtime(conn: Any) -> float:
    """The mtime of the offline artifact(s) a connection is seeded FROM, so the seed +
    refresh can RE-run when ANY changes (a fresh ``dbt parse``/``build``, a ``dbt docs
    generate`` that rewrites only catalog.json, a re-saved workbook). The MAX across every
    present artifact attr — so a connection with several (dbt: manifest.json + catalog.json)
    re-seeds when the NEWEST changes, not only the first. ``0.0`` when the connection has no
    artifact (seed-once) or all are absent. Best-effort: any stat failure is skipped."""
    mtimes: list[float] = []
    for attr in _ARTIFACT_ATTRS:
        value = conn.attributes.get(attr)
        if not value:
            continue
        path = Path(str(value))
        patterns = _DIR_ARTIFACT_ATTRS.get(attr)
        if patterns is not None:
            # A directory artifact: the NEWEST owned file's mtime (in-place edits register).
            mtimes.append(max((mt / 1e9 for _, mt, _ in _dir_entries(path, patterns)), default=0.0))
        else:
            with contextlib.suppress(OSError):
                mtimes.append(path.stat().st_mtime)
    return max(mtimes, default=0.0)


def artifact_fingerprint(conn: Any) -> str | None:
    """A cheap, persistable token (mtime_ns + size) for a connection's offline artifact —
    changes IFF the artifact changed. ``None`` when there is no artifact (a live warehouse)
    or it's absent, meaning "can't tell — always re-emit". The seed's skip-unchanged gate
    persists this so a daemon restart on an unchanged project doesn't re-emit the graph
    (the in-memory mtime gate doesn't survive a restart). A file's size complements mtime
    so a same-mtime content rewrite still re-emits."""
    for attr in _ARTIFACT_ATTRS:
        value = conn.attributes.get(attr)
        if not value:
            continue
        path = Path(str(value))
        patterns = _DIR_ARTIFACT_ATTRS.get(attr)
        if patterns is not None:
            # A directory artifact (Airflow dags/): a digest over every owned file's
            # (relpath, mtime_ns, size) — changes on an IN-PLACE edit OR add/remove, unlike the
            # directory's own stat (which only moves on add/remove, missing every file rewrite).
            entries = _dir_entries(path, patterns)
            if not entries and not path.is_dir():
                return None  # missing dir → can't tell → always re-emit
            digest = hashlib.sha256(
                "\n".join(f"{rel}:{mt}:{sz}" for rel, mt, sz in entries).encode()
            ).hexdigest()
            return f"dir:{digest[:32]}"
        with contextlib.suppress(OSError):
            st = path.stat()
            return f"{st.st_mtime_ns}:{st.st_size}"
    return None


def _norm(path: str | Path) -> Path:
    """Absolute, symlink-RESOLVED, normalized path. ``resolve()`` is non-strict, so a DELETED
    file still normalizes (its leaf needn't exist — we react to removals); resolving symlinks
    reconciles e.g. macOS's ``/var`` → ``/private/var`` so an OS watcher's real-path event
    matches a connection's configured path."""
    return Path(path).resolve()


@dataclass(frozen=True)
class WatchSpec:
    """The files + directories a connection's lineage/KB derives from — so editing, ADDING,
    or REMOVING one triggers that connection's refresh.

    ``files`` are matched EXACTLY (the artifact itself: a dbt ``manifest.json``, a
    ``.duckdb``, a Tableau workbook). ``dirs`` are watched recursively: any file under them
    whose name matches ``patterns`` (empty ⇒ every file) is owned — so a NEW or REMOVED file
    in the dir (e.g. an added/deleted Airflow DAG) counts, not only an edit to an existing one.
    """

    files: tuple[Path, ...] = ()
    dirs: tuple[Path, ...] = ()
    patterns: tuple[str, ...] = ()

    def watch_dirs(self) -> set[Path]:
        """The directories to hand a recursive OS watcher: each watched file's PARENT (so an
        atomic save-rename of the file is still caught) plus the dir roots themselves."""
        return {_norm(f).parent for f in self.files} | {_norm(d) for d in self.dirs}

    def is_artifact_file(self, path: str | Path) -> bool:
        """Whether ``path`` is one of the EXPLICIT artifact files (a dbt ``manifest.json``, a
        ``.duckdb``, a workbook). These are the connection's source of truth and are honored
        even when gitignored (dbt's ``target/`` usually IS gitignored) — distinct from a file
        merely under a watched dir, which ``.gitignore`` filters."""
        target = _norm(path)
        return any(target == _norm(f) for f in self.files)

    def matches(self, path: str | Path) -> bool:
        """Whether a changed ``path`` (added / modified / deleted) is owned by this spec: the
        exact artifact file, or a file under a watched dir matching a pattern."""
        target = _norm(path)
        if any(target == _norm(f) for f in self.files):
            return True
        for d in self.dirs:
            root = _norm(d)
            if target != root and root not in target.parents:
                continue
            if not self.patterns or any(fnmatch(target.name, pat) for pat in self.patterns):
                return True
        return False


def connection_watch_spec(conn: Any) -> WatchSpec | None:
    """The files/dirs a connection owns for watch purposes, or ``None`` for a live warehouse
    (no offline artifact). dbt → its compiled ``manifest.json`` (manifest-only: editing a
    model's ``.sql`` doesn't change lineage until a recompile rewrites the manifest);
    DuckDB/Tableau → their single file; Airflow → its ``dags/`` dir filtered to ``*.py``.

    Derived from the same ``_ARTIFACT_ATTRS`` the seed/refresh gate on, so ownership stays in
    one place — a plugin that adds an artifact attr is picked up here for free."""
    files: list[Path] = []
    dirs: list[Path] = []
    patterns: list[str] = []
    for attr in _ARTIFACT_ATTRS:
        value = conn.attributes.get(attr)
        if not value:
            continue
        path = Path(str(value))
        dir_patterns = _DIR_ARTIFACT_ATTRS.get(attr)
        if dir_patterns is not None:
            dirs.append(path)
            patterns.extend(dir_patterns)
        else:
            files.append(path)
    if not files and not dirs:
        return None
    return WatchSpec(files=tuple(files), dirs=tuple(dirs), patterns=tuple(dict.fromkeys(patterns)))


def match_connections(changed_path: str | Path, connections: Iterable[Any]) -> list[Any]:
    """Every connection that owns ``changed_path`` — a ONE-TO-MANY mapping (two connections
    backed by the same file both match), so the watcher nudges every affected refresh."""
    return [
        conn
        for conn in connections
        if (spec := connection_watch_spec(conn)) is not None and spec.matches(changed_path)
    ]


def workspace_relative(path: str | Path, workspace_root: Path | None) -> str:
    """A KB item's ``file_sources`` entry for ``path``, as a forward-slash WORKSPACE-relative
    string (so the read-side staleness check resolves it as ``workspace_root / rel``). Falls
    back to the absolute path when ``path`` is outside the workspace (a connection added by a
    path outside the project) or no root is known — ``Path('/ws') / '/abs'`` still resolves to
    the absolute file, so staleness keeps working and the UI shows the real location."""
    p = Path(path)
    if workspace_root is not None:
        with contextlib.suppress(ValueError):
            return p.resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    return p.as_posix()


__all__ = [
    "LIVE_WAREHOUSE_REFRESH_CADENCE_SECONDS",
    "OFFLINE_REFRESH_CADENCE_SECONDS",
    "WatchSpec",
    "artifact_fingerprint",
    "artifact_mtime",
    "connection_watch_spec",
    "match_connections",
    "workspace_relative",
]
