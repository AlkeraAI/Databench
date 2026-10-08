"""Fixtures shared by the Files test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock, SeededIdSource
from alkera_core.files.repo import FilesRepo
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.engine import make_files_engine, open_files_session
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg, _seed_org
from tests.files._kit.tla import (
    TLC_RUNTIME_MISSING,
    docker_responsive,
    provision_tla_tools_jar,
)
from tests.files._kit.tla import (
    tlc_runtime as probe_tlc_runtime,
)


@pytest.fixture(scope="session")
def tla_tools_jar() -> Path:
    """The pinned tla2tools.jar, installed into .cache/tla/ if it is not there already.

    Every suite that shells out to ``ops/scripts/tlc.sh`` depends on this: the runner
    resolves the jar by path and exits 2 when it is absent, so a suite that does not ask
    for it is green only when some earlier module happened to install it.
    """
    return provision_tla_tools_jar()


@pytest.fixture(scope="session")
def tlc_runtime() -> str:
    """``"java"`` or ``"docker"``: what a TLC run here uses. Skips when the host has
    neither, so a test that needs TLC is skipped with the reason instead of waiting
    on a Docker that does not answer."""
    found = probe_tlc_runtime()
    if found is None:
        pytest.skip(TLC_RUNTIME_MISSING)
    return found


@pytest.fixture(scope="session")
def tlc_in_docker() -> None:
    """For a test about the Docker path of the TLC runner itself."""
    if not docker_responsive():
        pytest.skip("needs a Docker that starts the JRE image (tests.files._kit.tla)")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(now=EPOCH)


@pytest.fixture
def ids() -> SeededIdSource:
    return SeededIdSource(1234)


@pytest.fixture
def checkpoints() -> PausingCheckpoints:
    return PausingCheckpoints()


@pytest.fixture(scope="session")
async def files_engine() -> AsyncIterator[AsyncEngine]:
    """The engine every Files test borrows a connection from.

    One per test session — which, under xdist, is one per worker process, each
    already pointed at its own database by the repo-root conftest. Disposed at
    the end of the session, exactly where the per-test engines were disposed
    before.
    """
    engine = make_files_engine()
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def files_session(files_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A real session on this run's database, on its own connection.

    ``expire_on_commit=False`` because the factories commit and then hand the
    rows back: an expired instance would re-load lazily on first attribute
    access, which under asyncio is an error rather than a query.
    """
    session = await open_files_session(files_engine)
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def files_org(files_session: AsyncSession) -> FilesOrg:
    return await _seed_org(files_session)


@pytest.fixture
def files_org_factory(files_session: AsyncSession) -> Callable[[], Awaitable[FilesOrg]]:
    """Seed another tenant, for the cross-org isolation tests."""

    async def make() -> FilesOrg:
        return await _seed_org(files_session)

    return make


@pytest.fixture
async def repo(files_session: AsyncSession, files_org: FilesOrg) -> FilesRepo:
    return FilesRepo(files_session, files_org.scope)


@pytest.fixture
def files_factory(files_session: AsyncSession, files_org: FilesOrg) -> FilesFactory:
    return FilesFactory(files_session, files_org)


#: Re-exported so an api-core Files module that still imports these from
#: ``conftest`` keeps working locally. Cross-tree it is ambiguous — the
#: canonical spelling is ``tests.files._kit.factory``.
__all__ = ["EPOCH", "FilesFactory", "FilesOrg"]
