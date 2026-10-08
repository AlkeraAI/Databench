"""The worker hands the lease reaper the deployment's unsynced grace.

The grace sweep itself -- facet-only rows trashed past the grace and not
before, landed files kept, one frame per folder or one subtree frame, the
reason on the trash entry, the same machine coming back -- is proven through
the real routes in ``apps/backend/tests/files/test_files_lease_unsynced.py``,
running the very ``LeaseReaper`` the janitor schedule runs. What is the
worker's own is the wiring: the janitor pass is the only thing that runs the
sweep, so a grace the deployment configured and the worker never passed on is
a day that is silently some other length.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from alkera_core.config import settings as process_settings
from alkera_core.files.sweepers import JANITOR_ORDER, LeaseReaper
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_files_bootstrap import _files_settings, forget_org_with_a_drive, seed_org_with_a_drive
from worker.files_bootstrap import wire_files_jobs
from worker.tasks import files as tasks


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(process_settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()
        tasks.reset_deps_factory()


@pytest.fixture
async def org(db: AsyncSession) -> AsyncIterator[uuid.UUID]:
    org_team_id = await seed_org_with_a_drive(db)
    try:
        yield org_team_id
    finally:
        await forget_org_with_a_drive(db, org_team_id)


@pytest.mark.parametrize(
    "seconds",
    [pytest.param(7, id="a-short-grace"), pytest.param(3 * 86_400, id="a-long-grace")],
)
async def test_the_janitors_reaper_gets_the_configured_grace(
    tmp_path: Path, db: AsyncSession, org: uuid.UUID, seconds: int
) -> None:
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True,
            files_store_provider="filesystem",
            files_store_root=tmp_path,
            files_unsynced_grace_seconds=seconds,
        )
    )

    deps = await tasks._deps(db, org)

    assert deps.sweep is not None
    assert deps.sweep.unsynced_grace == timedelta(seconds=seconds)


def test_the_reaper_rides_the_janitor_pass() -> None:
    """No schedule of its own: the grace sweep is a step of the lease reaper,
    and the reaper is a step of every janitor pass."""
    assert LeaseReaper in JANITOR_ORDER
