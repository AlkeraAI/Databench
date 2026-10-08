"""Discovery of a LOCALLY-installed ``claude`` CLI
(env → $PATH → known install locations → claude-agent-sdk bundle). PATH-aware on
purpose: we never ship the proprietary binary, so the Claude-agent harness drives
the user's own install (or skips when there isn't one)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alkera_cli.harness import claude_binary
from alkera_cli.harness.claude_binary import (
    ClaudeBinaryNotFoundError,
    claude_is_available,
    find_claude_binary,
    resolve_claude_binary,
)


def _make_exe(path: Path) -> Path:
    path.write_text("#!/bin/sh\necho 2.1.0\n")
    path.chmod(0o755)
    return path


def _force_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_binary, "_known_locations", list)
    monkeypatch.setattr(claude_binary, "_sdk_bundled_path", lambda: None)


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _make_exe(tmp_path / "claude")
    monkeypatch.setenv("ALKERA_CLAUDE_BIN", str(exe))
    r = find_claude_binary()
    assert r is not None and r.source == "env" and r.path == exe.absolute()


def test_path_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_CLAUDE_BIN", raising=False)
    exe = _make_exe(tmp_path / "claude")
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: str(exe))
    r = find_claude_binary()
    assert r is not None and r.source == "path" and r.path == exe.absolute()


def test_known_location_when_not_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: None)
    exe = _make_exe(tmp_path / "claude")
    monkeypatch.setattr(claude_binary, "_known_locations", lambda: [tmp_path / "nope", exe])
    r = find_claude_binary()
    assert r is not None and r.source == "known"


def test_sdk_bundle_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALKERA_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_binary, "_known_locations", list)
    exe = _make_exe(tmp_path / "claude")
    monkeypatch.setattr(claude_binary, "_sdk_bundled_path", lambda: exe)
    r = find_claude_binary()
    assert r is not None and r.source == "sdk"


def test_real_sdk_bundle_present_after_uv_sync() -> None:
    # In any uv-synced dev/CI env the claude-agent-sdk wheel bundles a claude, so
    # the harness is discoverable out of the box (no separate install / CI step).
    assert claude_binary._sdk_bundled_path() is not None


def test_not_installed_returns_none_and_resolve_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_not_installed(monkeypatch)
    assert find_claude_binary() is None
    assert claude_is_available() is False
    with pytest.raises(ClaudeBinaryNotFoundError):
        resolve_claude_binary()


def test_available_true_when_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALKERA_CLAUDE_BIN", str(_make_exe(tmp_path / "claude")))
    assert claude_is_available() is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod-based executability is POSIX; on Windows os.access(X_OK) is just existence",
)
def test_non_executable_env_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "not-exec"
    bad.write_text("x")  # not chmod +x
    monkeypatch.setenv("ALKERA_CLAUDE_BIN", str(bad))
    monkeypatch.setattr(claude_binary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_binary, "_known_locations", list)
    monkeypatch.setattr(claude_binary, "_sdk_bundled_path", lambda: None)
    assert find_claude_binary() is None


def test_claude_filename_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_binary.sys, "platform", "win32")
    assert claude_binary._claude_filename() == "claude.exe"
    monkeypatch.setattr(claude_binary.sys, "platform", "darwin")
    assert claude_binary._claude_filename() == "claude"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake claude is a #!/bin/sh script — not executable on Windows",
)
def test_version_probe_best_effort(tmp_path: Path) -> None:
    assert claude_binary._probe_version(_make_exe(tmp_path / "claude")) == "2.1.0"
    assert claude_binary._probe_version(Path("/nonexistent/xyz")) is None
