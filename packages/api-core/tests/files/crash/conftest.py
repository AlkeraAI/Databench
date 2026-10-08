"""Fixtures for the Files crash harness."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """An empty directory the child process owns for one scenario run."""
    directory = tmp_path / "crash"
    directory.mkdir()
    return directory
