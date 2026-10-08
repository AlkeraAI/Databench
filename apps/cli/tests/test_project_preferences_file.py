"""The per-project preferences file (`.alkera/preferences.yml`) — defaults on a
missing/corrupt file, lock-guarded merge writes, and forward-compat preservation."""

from __future__ import annotations

from pathlib import Path

from alkera_cli.preferences.project import (
    load_project_preferences,
    update_project_preferences,
)
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.project_preferences import ProjectPreferences


def _project(tmp_path: Path) -> ProjectDirectory:
    return ProjectDirectory(tmp_path / ".alkera")


def test_missing_file_reads_defaults(tmp_path: Path):
    prefs = load_project_preferences(_project(tmp_path))
    assert prefs.kb_sync_enabled is None  # inherit org


def test_update_persists_and_reloads(tmp_path: Path):
    project = _project(tmp_path)
    update_project_preferences(project, lambda p: p.model_copy(update={"kb_sync_enabled": True}))
    assert load_project_preferences(project).kb_sync_enabled is True
    # The file lives in the project's .alkera/, not the user home.
    assert project.preferences_path.exists()
    assert project.preferences_path.name == "preferences.yml"


def test_update_merges_unknown_fields(tmp_path: Path):
    """A field a newer writer persisted survives an older reader's merge-write
    (the read-modify-write doesn't clobber what it can't type)."""
    project = _project(tmp_path)
    project.preferences_path.write_text(
        "schema_version: 1.0.0\nkb_sync_enabled: false\nfuture_flag: keep\n"
    )
    update_project_preferences(project, lambda p: p.model_copy(update={"kb_sync_enabled": True}))
    reread = project.preferences_path.read_text()
    assert "future_flag: keep" in reread
    assert load_project_preferences(project).kb_sync_enabled is True


def test_corrupt_file_reads_defaults(tmp_path: Path):
    project = _project(tmp_path)
    project.preferences_path.write_text("{ this is not: valid yaml: : :")
    assert load_project_preferences(project).kb_sync_enabled is None


def test_set_false_distinct_from_inherit(tmp_path: Path):
    """An explicit False (opt-out) is distinct from None (inherit) on reload."""
    project = _project(tmp_path)
    update_project_preferences(project, lambda p: ProjectPreferences(kb_sync_enabled=False))
    assert load_project_preferences(project).kb_sync_enabled is False
