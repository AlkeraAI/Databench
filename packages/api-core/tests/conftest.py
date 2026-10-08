"""Shared fixtures for the api-core test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from _settings_env import seal_settings_env


@pytest.fixture
def require_effective_chmod(tmp_path: Path) -> None:
    """Skip when tmp_path's filesystem doesn't honor chmod mode bits — e.g.
    WSL DrvFs (/mnt/c) without the metadata mount option, where every file
    stats as 0o777 and a read-only dir doesn't block writes. The product state
    these tests model lives on a POSIX filesystem even under WSL; only
    workspace files land on DrvFs."""
    probe = tmp_path / ".chmod-probe"
    probe.write_text("x")
    probe.chmod(0o600)
    if (probe.stat().st_mode & 0o777) != 0o600:
        pytest.skip("filesystem does not honor chmod mode bits (WSL DrvFs?)")


@pytest.fixture
def sealed_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture form of :func:`_settings_env.seal_settings_env` — see it for the why."""
    seal_settings_env(monkeypatch)
