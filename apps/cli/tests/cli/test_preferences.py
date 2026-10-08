"""Tests for `alkera_cli.preferences.user` — the `~/.alkera/preferences.yml`
read/write helper.

Isolates `~/.alkera` to a tmp dir by monkeypatching the `paths` module
constants (the same idiom as `test_daemon_auth.py:_isolate_home`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from alkera_cli.host import paths
from alkera_cli.preferences import user as preferences_file
from alkera_core.project import FileLock
from alkera_core.schemas.preferences import DesktopPreferences, Preferences


@pytest.fixture(autouse=True)
def isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "alkera-home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", home / "preferences.yml")
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", home / ".preferences.lock")
    return home


def test_load_missing_returns_defaults() -> None:
    prefs = preferences_file.load_preferences()
    assert prefs.show_banner is True


def test_save_then_load_round_trip() -> None:
    preferences_file.save_preferences(Preferences(show_banner=False))
    assert preferences_file.load_preferences().show_banner is False


def test_saved_file_is_yaml_with_version_stamp(isolate_home: Path) -> None:
    preferences_file.save_preferences(Preferences(show_banner=False))
    data = yaml.safe_load((isolate_home / "preferences.yml").read_text())
    assert data["show_banner"] is False
    assert "show_thinking" not in data
    assert data["schema_version"] == DesktopPreferences.SCHEMA_VERSION


def test_update_preferences_read_modify_write() -> None:
    preferences_file.save_preferences(Preferences(show_banner=True))
    updated = preferences_file.update_preferences(
        lambda p: p.model_copy(update={"show_banner": False})
    )
    assert updated.show_banner is False
    assert preferences_file.load_preferences().show_banner is False


@pytest.mark.parametrize("raw", ["true", "True", "on", "1", "yes", "y"])
def test_set_preference_bool_truthy(raw: str) -> None:
    result = preferences_file.set_preference("show_banner", raw)
    assert result.show_banner is True
    assert preferences_file.load_preferences().show_banner is True


@pytest.mark.parametrize("raw", ["false", "False", "off", "0", "no", "n"])
def test_set_preference_bool_falsey(raw: str) -> None:
    preferences_file.save_preferences(Preferences(show_banner=True))
    result = preferences_file.set_preference("show_banner", raw)
    assert result.show_banner is False


def test_set_preference_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="unknown preference"):
        preferences_file.set_preference("nope", "true")


def test_removed_show_thinking_is_rejected_by_flat_setter() -> None:
    """Catch a stale setter registry that keeps the removed independent switch
    writable even though reasoning effort is now the sole thinking choice."""
    assert "show_thinking" not in preferences_file.settable_fields()
    with pytest.raises(ValueError, match="unknown preference"):
        preferences_file.set_preference("show_thinking", "true")


@pytest.mark.parametrize("key", ["onboarded_cli", "onboarded_extension"])
def test_onboarding_flags_are_not_flat_settable(key: str) -> None:
    """The onboarding lifecycle markers are written by the flows themselves
    (finish/skip/replay), never via `/prefs set` — a flat-settable flag would
    let a stray command half-reset the flow."""
    assert key not in preferences_file.settable_fields()
    with pytest.raises(ValueError, match="unknown preference"):
        preferences_file.set_preference(key, "true")


def test_set_preference_bad_bool_value_raises() -> None:
    with pytest.raises(ValueError, match="true/false"):
        preferences_file.set_preference("show_banner", "maybe")


def test_unknown_field_survives_update(isolate_home: Path) -> None:
    """A field a newer writer added is preserved when this version does a
    read-modify-write (extra='allow')."""
    (isolate_home / "preferences.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "show_thinking": False,
                "future_pref": "keep-me",
            }
        )
    )
    preferences_file.set_preference("show_banner", "false")
    data = yaml.safe_load((isolate_home / "preferences.yml").read_text())
    assert data["future_pref"] == "keep-me"
    assert data["show_banner"] is False
    assert "show_thinking" not in data


def test_atomic_write_leaves_no_temp_files(isolate_home: Path) -> None:
    preferences_file.save_preferences(Preferences(show_banner=False))
    leftovers = [
        p.name
        for p in isolate_home.iterdir()
        if p.name.startswith("preferences.yml.") and p.name.endswith(".tmp")
    ]
    assert leftovers == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_saved_file_mode_is_0600(isolate_home: Path) -> None:
    preferences_file.save_preferences(Preferences())
    mode = (isolate_home / "preferences.yml").stat().st_mode & 0o777
    assert mode == 0o600


def test_concurrent_writer_blocks_then_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If another live process holds the lock past the timeout, a write
    raises rather than silently corrupting the file."""
    monkeypatch.setattr(preferences_file, "_LOCK_TIMEOUT_SECONDS", 0.1)
    held = FileLock(paths.PREFERENCES_LOCK_PATH)
    held.acquire()
    try:
        from alkera_core.project import LockHeldError

        with pytest.raises(LockHeldError):
            preferences_file.save_preferences(Preferences(show_banner=False))
    finally:
        held.release()
