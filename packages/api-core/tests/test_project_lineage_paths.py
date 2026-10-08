"""ProjectDirectory's runs/lineage path accessors."""

from __future__ import annotations

from pathlib import Path

from alkera_core.project import ProjectDirectory


def test_path_accessors_resolve_under_alkera_root(tmp_path: Path) -> None:
    root = tmp_path / ".alkera"
    project = ProjectDirectory(root)
    assert project.runs_path == root / "runs"
    assert project.lineage_path == root / "lineage"
    assert project.run_dir("run1") == root / "runs" / "run1"
    assert project.run_lineage_log_path("run1") == root / "runs" / "run1" / "lineage.jsonl"


def test_initialize_creates_lineage_dir_but_not_runs(tmp_path: Path) -> None:
    root = tmp_path / ".alkera"
    project = ProjectDirectory(root)
    # lineage indexes dir is created eagerly; per-run dirs are created lazily.
    assert project.lineage_path.is_dir()
    assert not project.runs_path.exists()
