"""Filesystem discovery helpers shared by the local connector plugins.

Connection discovery walks the workspace tree for marker files — a
``dbt_project.yml`` (a dbt project), a ``*.duckdb`` file (a local warehouse). A
naive ``Path.rglob`` descends into ``node_modules`` / ``.git`` / ``.venv`` / the
``.alkera`` state dir, which is slow on a real repo and surfaces vendored or
generated files as phantom connections. ``find_files`` prunes those directories
wholesale and never follows symlinks (so a cyclic link can't hang discovery).
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

#: Directory basenames never worth descending into for either connection discovery
#: OR plugin-activation marker scanning — VCS metadata, virtualenvs, dependency
#: trees, language caches, build output, editor state, and the ``.alkera`` workspace
#: dir itself. ONE shared set so the two scans can't diverge (a project that
#: activates but whose connection then can't be discovered, or vice versa). Pruned
#: wholesale by basename.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        # `dbt deps` vendors third-party dbt PACKAGES here, each a full dbt project with
        # its own dbt_project.yml — pruning stops those vendored copies surfacing as
        # phantom connections (the dbt analogue of node_modules).
        "dbt_packages",
        ".alkera",
        # A source checkout vendors third-party trees here (e.g. the ~6 GB opencode
        # subtree) — descending into it makes every discovery walk crawl and surfaces
        # vendored projects as phantom connections.
        "vendor",
    }
)


def find_files(root: Path, pattern: str) -> list[Path]:
    """Every file under ``root`` whose name matches ``pattern`` (an ``fnmatch`` glob
    such as ``*.duckdb`` or ``dbt_project.yml``), skipping :data:`IGNORED_DIRS`.

    Results are sorted for deterministic discovery order. Symlinked directories are
    not descended into (``os.walk`` default), so a cyclic symlink can't hang the
    scan."""
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # In-place prune so os.walk never descends into the ignored trees.
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                matches.append(Path(dirpath) / name)
    return sorted(matches)


def find_dirs(root: Path, pattern: str) -> list[Path]:
    """Every DIRECTORY under ``root`` whose basename matches ``pattern`` (an
    ``fnmatch`` glob such as ``dags``), skipping :data:`IGNORED_DIRS`. The matched
    directory itself is NOT pruned from the results even if a deeper ignored dir
    would be. Sorted; symlinked dirs are not descended into.

    Used to locate convention-named project roots (an Airflow ``dags/`` folder)
    that a file-name glob can't find."""
    matches: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in list(dirnames):
            if fnmatch.fnmatch(name, pattern):
                matches.append(Path(dirpath) / name)
    return sorted(matches)


def describe_location(path: Path, workspace_root: Path) -> str:
    """A short, human-readable ``relative/path`` of ``path`` within the workspace
    (``"the workspace root"`` for the root itself) — the ``discovered_from`` hint the
    Plugins & Connections suggestions feed shows so a user knows why a candidate is
    offered. Falls back to the absolute path if ``path`` is outside the workspace."""
    try:
        rel = path.relative_to(workspace_root)
    except ValueError:
        return str(path)
    rel_str = str(rel)
    return "the workspace root" if rel_str == "." else rel_str


__all__ = ["IGNORED_DIRS", "describe_location", "find_dirs", "find_files"]
