"""Trace + summary path helpers: the run/node path layout and the
path-component guard that keeps an engine-minted id from escaping the run dir."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.project import ProjectDirectory


def _project(tmp_path: Path) -> ProjectDirectory:
    return ProjectDirectory(tmp_path / ".alkera")


def test_trace_streams_live_under_traces_tree(tmp_path: Path) -> None:
    project = _project(tmp_path)
    root = (tmp_path / ".alkera").resolve()
    assert project.run_trace_path("r1") == root / "traces" / "r1" / "run.jsonl"
    assert project.coordinator_trace_path("r1") == root / "traces" / "r1" / "coordinator.jsonl"
    assert project.node_trace_path("r1", "n1") == root / "traces" / "r1" / "nodes" / "n1.jsonl"


def test_summaries_live_under_runs_tree_beside_lineage(tmp_path: Path) -> None:
    project = _project(tmp_path)
    root = (tmp_path / ".alkera").resolve()
    assert project.run_summary_path("r1") == root / "runs" / "r1" / "run.yaml"
    assert project.node_summary_path("r1", "n1") == root / "runs" / "r1" / "n1.yaml"
    # The lineage log is a sibling of the summaries, not under traces/.
    assert project.run_lineage_log_path("r1").parent == project.run_summary_path("r1").parent


@pytest.mark.parametrize(
    "bad", ["../../outside", "a/b", "..", ".", ".hidden", "x\\y", "a\x00b", ""]
)
def test_traversal_node_id_is_rejected(tmp_path: Path, bad: str) -> None:
    project = _project(tmp_path)
    with pytest.raises(ValueError):
        project.node_summary_path("r1", bad)
    with pytest.raises(ValueError):
        project.node_trace_path("r1", bad)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ""])
def test_traversal_run_id_is_rejected(tmp_path: Path, bad: str) -> None:
    project = _project(tmp_path)
    with pytest.raises(ValueError):
        project.run_dir(bad)
    with pytest.raises(ValueError):
        project.trace_dir(bad)


def test_clean_ids_stay_inside_the_run_directory(tmp_path: Path) -> None:
    project = _project(tmp_path)
    base = (project.runs_path / "r1").resolve()
    path = project.node_summary_path("r1", "node-1.abc")
    assert path.resolve().is_relative_to(base)
    assert path.name == "node-1.abc.yaml"


def test_artifact_and_worktree_paths_match_the_canonical_layout(tmp_path: Path) -> None:
    project = _project(tmp_path)
    root = (tmp_path / ".alkera").resolve()
    assert project.artifacts_path == root / "artifacts" / "objects" / "sha256"
    assert project.worktrees_path == root / "worktrees"
    assert project.run_worktree_dir("r1") == root / "worktrees" / "r1"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ""])
def test_run_worktree_dir_rejects_a_traversal_id(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        _project(tmp_path).run_worktree_dir(bad)
