"""``existing_project_directory``: the workspace at a folder, or none, and
reading never creates one."""

from __future__ import annotations

from pathlib import Path

from alkera_cli.host.paths import PROJECT_STATE_DIRNAME, existing_project_directory
from alkera_core.project import ProjectDirectory


def test_no_folder_is_no_workspace() -> None:
    assert existing_project_directory(None) is None


def test_a_folder_nothing_has_run_in_is_no_workspace_and_stays_that_way(tmp_path: Path) -> None:
    assert existing_project_directory(tmp_path) is None
    assert not (tmp_path / PROJECT_STATE_DIRNAME).exists(), "reading must not create .alkera/"


def test_a_folder_with_state_is_its_workspace(tmp_path: Path) -> None:
    ProjectDirectory(tmp_path / PROJECT_STATE_DIRNAME)

    found = existing_project_directory(tmp_path)

    assert found is not None and found.path == tmp_path / PROJECT_STATE_DIRNAME
