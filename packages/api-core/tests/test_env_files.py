"""Where the dotenv files are read from (``alkera_core.env_files``).

A process may start at a checkout's root, in an app directory below it, or in the
open tree a private checkout carries; it reads the root's files in every case,
never a ``.env`` above the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _settings_env import seal_settings_env
from alkera_core.config import Settings
from alkera_core.env_files import env_dir, env_files

pytestmark = [pytest.mark.spread]


def _tree(root: Path, *files: str) -> None:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("/.git"):
            path.mkdir()
        else:
            path.write_text("", encoding="utf-8")


def test_the_nearest_root_with_an_env_file_is_found_from_below(tmp_path: Path) -> None:
    _tree(tmp_path, "repo/.git", "repo/.env", "repo/Databench/apps/backend/x.py")
    start = tmp_path / "repo/Databench/apps/backend"
    assert env_dir(start, {}) == (tmp_path / "repo").resolve()


@pytest.mark.parametrize("marker", [".env", ".env.workspace"])
def test_either_marker_names_the_root(tmp_path: Path, marker: str) -> None:
    _tree(tmp_path, f"repo/{marker}", "repo/apps/cli/y.py")
    assert env_dir(tmp_path / "repo/apps/cli", {}) == (tmp_path / "repo").resolve()


def test_a_dotenv_above_the_repository_is_never_read(tmp_path: Path) -> None:
    _tree(tmp_path, ".env", "repo/.git", "repo/apps/z.py")
    start = tmp_path / "repo/apps"
    assert env_dir(start, {}) == start.resolve()


def test_with_no_marker_the_working_directory_is_used(tmp_path: Path) -> None:
    assert env_dir(tmp_path, {}) == tmp_path.resolve()


def test_an_explicit_directory_wins(tmp_path: Path) -> None:
    _tree(tmp_path, "repo/.env")
    chosen = tmp_path / "elsewhere"
    assert env_dir(tmp_path / "repo", {"ALKERA_ENV_DIR": str(chosen)}) == chosen
    assert env_files(tmp_path / "repo", {"ALKERA_ENV_DIR": str(chosen)}) == (
        chosen / ".env",
        chosen / ".env.workspace",
        chosen / ".env.local",
    )


def test_settings_built_below_the_root_read_the_roots_files_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal_settings_env(monkeypatch)
    monkeypatch.delenv("ALKERA_ENV_DIR", raising=False)
    root = tmp_path / "repo"
    _tree(root, ".git", "apps/backend/x.py")
    (root / ".env").write_text("LOG_LEVEL=WARNING\nMETRICS_ENABLED=false\n", encoding="utf-8")
    (root / ".env.local").write_text("LOG_LEVEL=ERROR\n", encoding="utf-8")
    monkeypatch.chdir(root / "apps/backend")
    settings = Settings()
    assert settings.log_level == "ERROR"
    assert settings.metrics_enabled is False


def test_named_files_still_override_the_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seal_settings_env(monkeypatch)
    root = tmp_path / "repo"
    _tree(root, ".git")
    (root / ".env").write_text("LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert Settings(_env_file=None).log_level == "INFO"
