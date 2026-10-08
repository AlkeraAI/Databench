"""Tests for the daemon's `maintenance.cleanupCache` method.

The wire/stream plumbing is identical to (and covered by)
``test_daemon_preferences.py``; here we focus on the method being registered
and the handler's behaviour — active-SHA protection, stale removal, and the
fallback when no binary resolves. ``~/.alkera/`` is isolated to a tmp dir so
the real user cache is never touched.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alkera_cli.daemon.methods import maintenance
from alkera_cli.daemon.protocol import METHODS
from alkera_cli.host import paths


def test_method_is_registered() -> None:
    assert "maintenance.cleanupCache" in METHODS


def _seed_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *shas: str) -> Path:
    alkera_home = tmp_path / "alkera-home"
    root = alkera_home / "cache"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "ALKERA_HOME", alkera_home)
    for sha in shas:
        d = root / f"runtime-{sha}"
        d.mkdir()
        (d / "alkera-agent").write_text("x")
        os.utime(d, (0.0, 0.0))  # epoch → far older than the 14-day window
    return root


def test_handler_protects_active_and_removes_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_cache(monkeypatch, tmp_path, "active", "stale")

    class _Resolved:
        sha = "active"

    monkeypatch.setattr(maintenance, "resolve_opencode_binary", lambda: _Resolved())

    resp = asyncio.run(
        maintenance.maintenance_cleanup_cache(
            object(),  # server is unused by this handler
            maintenance.MaintenanceCleanupCacheRequest(),
        )
    )

    assert resp.removed == ["stale"]
    assert resp.kept_count == 1
    assert resp.errors == []
    assert (root / "runtime-active").exists()
    assert not (root / "runtime-stale").exists()


def test_handler_falls_back_to_no_keep_sha_when_binary_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev checkout with no built binary → resolution raises → cleanup runs
    on pure staleness with no protected SHA."""
    root = _seed_cache(monkeypatch, tmp_path, "orphan")

    def _raise() -> object:
        raise maintenance.OpencodeBinaryNotFoundError(attempted=["none"])

    monkeypatch.setattr(maintenance, "resolve_opencode_binary", _raise)

    resp = asyncio.run(
        maintenance.maintenance_cleanup_cache(
            object(), maintenance.MaintenanceCleanupCacheRequest()
        )
    )

    assert resp.removed == ["orphan"]
    assert not (root / "runtime-orphan").exists()
