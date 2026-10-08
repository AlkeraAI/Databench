"""Tests for the global-instructions local store (`alkera_cli.preferences.instructions`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_cli.host import paths
from alkera_cli.preferences import instructions as instructions_file


@pytest.fixture
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "INSTRUCTIONS_FILE_PATH", home / "instructions.md")
    monkeypatch.setattr(paths, "INSTRUCTIONS_LOCK_PATH", home / ".instructions.lock")
    return home


def test_load_returns_empty_when_missing(_home: Path) -> None:
    assert instructions_file.load_global_instructions() == ""


def test_save_then_load_round_trips(_home: Path) -> None:
    content = "# My rules\n\n- Always use type hints.\n"
    assert instructions_file.save_global_instructions(content) == content
    assert instructions_file.load_global_instructions() == content
    # The file is created on first save.
    assert (_home / "instructions.md").read_text(encoding="utf-8") == content


def test_save_empty_clears(_home: Path) -> None:
    instructions_file.save_global_instructions("something")
    assert instructions_file.save_global_instructions("") == ""
    assert instructions_file.load_global_instructions() == ""


def test_save_overwrites_wholesale(_home: Path) -> None:
    instructions_file.save_global_instructions("first")
    instructions_file.save_global_instructions("second")
    assert instructions_file.load_global_instructions() == "second"


def test_cap_passes_through_small_content() -> None:
    assert instructions_file.cap_global_instructions("short") == "short"


def test_cap_head_truncates_oversized_content_with_marker() -> None:
    big = "A" * (40 * 1024)
    capped = instructions_file.cap_global_instructions(big)
    assert len(capped.encode("utf-8")) < 33 * 1024
    assert capped.startswith("A")
    assert "global instructions truncated: showing first 32KB of 40KB" in capped


def test_cap_respects_custom_limit() -> None:
    capped = instructions_file.cap_global_instructions("x" * 100, limit=10)
    assert capped.startswith("x")
    assert "truncated" in capped


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode bits")
def test_saved_file_mode_is_0600(_home: Path) -> None:
    """The instructions file holds user prompt content shipped to the model — it must
    be private (0600), matching the preferences store it mirrors."""
    instructions_file.save_global_instructions("secret-ish guidance")
    assert (paths.INSTRUCTIONS_FILE_PATH.stat().st_mode & 0o777) == 0o600
