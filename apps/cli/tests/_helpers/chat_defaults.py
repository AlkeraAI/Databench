"""Shared setup for the saved-default convergence tests: a reader's
preferences file under a throwaway home, the cases every surface runs, and the
check they all make."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.host import paths

OPUS = GatewayModel(
    id="opus",
    display_name="Opus",
    wire="anthropic",
    efforts=("low", "medium", "high"),
    default_effort="high",
)

# (saved model, saved effort) -> (stored model, stored effort) after a run.
CASES = [
    pytest.param(("retired", "high"), (None, None), id="a-retired-pick-is-cleared"),
    pytest.param(("opus", "ultra"), ("opus", "high"), id="an-unoffered-effort-is-re-derived"),
    pytest.param(("opus", "medium"), ("opus", "medium"), id="a-valid-pick-is-left-alone"),
]


def use_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the preferences file at a fresh home under ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ALKERA_HOME", home)
    monkeypatch.setattr(paths, "PREFERENCES_FILE_PATH", home / "preferences.yml")
    monkeypatch.setattr(paths, "PREFERENCES_LOCK_PATH", home / ".preferences.lock")
    return home


def write_prefs(home: Path, model: str | None, effort: str | None) -> None:
    (home / "preferences.yml").write_text(
        yaml.safe_dump(
            {"schema_version": "1.4.0", "default_chat_model": model, "default_chat_effort": effort}
        )
    )


def stored(home: Path) -> tuple[str | None, str | None]:
    data = yaml.safe_load((home / "preferences.yml").read_text())
    return data["default_chat_model"], data["default_chat_effort"]


def assert_converged(
    home: Path,
    before: bytes,
    saved: tuple[str | None, str | None],
    expected: tuple[str | None, str | None],
) -> None:
    assert stored(home) == expected
    if saved == expected:
        # Nothing was stale, so nothing is written: the file is byte-identical
        # (a rewrite would restamp it at the current schema version).
        assert (home / "preferences.yml").read_bytes() == before
