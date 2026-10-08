"""The shared workspace-scan helper for connector discovery — prunes junk dirs
(node_modules / .git / .venv / .alkera) so discovery is fast and never surfaces
vendored or generated files as phantom connections."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.fs_discovery import (
    IGNORED_DIRS,
    describe_location,
    find_dirs,
    find_files,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_find_files_matches_nested_and_sorts(tmp_path: Path) -> None:
    _touch(tmp_path / "a.duckdb")
    _touch(tmp_path / "sub" / "deep" / "b.duckdb")
    _touch(tmp_path / "sub" / "c.txt")  # non-match
    found = find_files(tmp_path, "*.duckdb")
    assert found == sorted(found)  # deterministic order
    assert [p.name for p in found] == ["a.duckdb", "b.duckdb"]


@pytest.mark.parametrize("junk", sorted(IGNORED_DIRS))
def test_find_files_prunes_each_ignored_dir(tmp_path: Path, junk: str) -> None:
    # A marker file buried in any ignored dir must NOT surface.
    _touch(tmp_path / junk / "buried.duckdb")
    _touch(tmp_path / "real.duckdb")
    found = find_files(tmp_path, "*.duckdb")
    assert [p.name for p in found] == ["real.duckdb"]


def test_find_files_prunes_nested_ignored_dir(tmp_path: Path) -> None:
    # Pruning applies at any depth, not just the top level.
    _touch(tmp_path / "pkg" / "node_modules" / "dep" / "vendored.duckdb")
    _touch(tmp_path / "pkg" / "app.duckdb")
    assert [p.name for p in find_files(tmp_path, "*.duckdb")] == ["app.duckdb"]


def test_find_files_exact_name_pattern(tmp_path: Path) -> None:
    _touch(tmp_path / "proj" / "dbt_project.yml")
    _touch(tmp_path / "other" / "dbt_project.yaml")  # different extension, no match
    found = find_files(tmp_path, "dbt_project.yml")
    assert [p.parent.name for p in found] == ["proj"]


def test_find_dirs_matches_by_basename_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "proj_a" / "dags").mkdir(parents=True)
    (tmp_path / "proj_b" / "nested" / "dags").mkdir(parents=True)
    (tmp_path / "proj_a" / "models").mkdir()  # non-match basename
    found = find_dirs(tmp_path, "dags")
    assert found == sorted(found)
    assert [str(p.relative_to(tmp_path)) for p in found] == [
        str(Path("proj_a") / "dags"),
        str(Path("proj_b") / "nested" / "dags"),
    ]


def test_find_dirs_prunes_ignored_dirs(tmp_path: Path) -> None:
    # A `dags` dir buried in a vendored/ignored tree is not surfaced (the 5.9 GB
    # vendor walk that froze discovery is exactly this case).
    (tmp_path / "vendor" / "opencode" / "dags").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "dags").mkdir(parents=True)
    (tmp_path / "real" / "dags").mkdir(parents=True)
    found = find_dirs(tmp_path, "dags")
    assert [str(p.relative_to(tmp_path)) for p in found] == [str(Path("real") / "dags")]


def test_describe_location_root_and_nested(tmp_path: Path) -> None:
    assert describe_location(tmp_path, tmp_path) == "the workspace root"
    assert describe_location(tmp_path / "a" / "b", tmp_path) == str(Path("a") / "b")


def test_describe_location_outside_workspace_falls_back_to_abs(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere" / "x.duckdb"
    assert describe_location(outside, tmp_path) == str(outside)


def test_find_files_does_not_follow_symlink_loop(tmp_path: Path) -> None:
    # The documented anti-hang invariant: a self-referential symlink must not be
    # descended into. A marker reachable ONLY through the loop is never surfaced, and
    # the scan terminates (the test itself hanging would be the failure).
    _touch(tmp_path / "real.duckdb")
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(sub / "behind_link.duckdb")
    try:
        (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    found = {p.name for p in find_files(tmp_path, "*.duckdb")}
    # The real files surface exactly once; the loop is never traversed.
    assert found == {"real.duckdb", "behind_link.duckdb"}
    paths = find_files(tmp_path, "*.duckdb")
    assert not any("loop" in p.parts for p in paths)
