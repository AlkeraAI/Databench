"""Fixtures for the generated cross-tenant matrix and the per-area suites.

``world`` is the two-org identity with every area's seeds in both orgs, multi-org
on for the test, Files on behind a filesystem store under ``tmp_path`` (the same
production seam the Files suites install), and operations inline so nothing
waits on a worker the suite does not run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.scoped import FilesystemScoped
from backend.services.files.store import set_store_factory
from route_matrix import TwoOrgWorld, build_world
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import TwoOrg


@pytest.fixture
def files_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "files-store"
    root.mkdir()
    set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)
    monkeypatch.setattr(settings, "files_inline_operations", True)
    yield
    set_store_factory(None)


@pytest_asyncio.fixture
async def world(
    real_session: AsyncSession,
    two_org_identity: TwoOrg,
    multi_org: None,
    files_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> TwoOrgWorld:
    # The matrix fires one request per operation from one address; a 429 would
    # hide the answer under test.
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    return await build_world(real_session, two_org_identity)
