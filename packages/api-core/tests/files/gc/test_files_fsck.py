"""fsck refuses a holder facet with nothing behind it.

A file carries the holder's report only while a machine holds its folder, or
for the grace after it stopped. A facet still standing past that is a row
telling every reader bytes are on their way from a machine that is gone for
good: fsck names it, never repairs it (the lease reaper's grace sweep does),
and never puts it in front of an operator.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.config import settings
from alkera_core.files import fsck as fsck_mod
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import DomainId
from alkera_core.files.store.filesystem import FilesystemStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

GRACE = settings.files_unsynced_grace_seconds


@pytest.fixture
async def world(files_factory: Any, files_session: AsyncSession) -> dict[str, Any]:
    drive = await files_factory.drive()
    nodes = await files_factory.tree(
        "held/ held/deep/ held/deep/a.bin held/b.bin other.bin", drive=drive
    )
    await files_session.execute(
        text("UPDATE file_nodes SET holder_size = 5, holder_mtime_ns = 1 WHERE id = ANY(:ids)"),
        {"ids": [nodes[p].id for p in ("held/deep/a.bin", "held/b.bin", "other.bin")]},
    )
    await files_session.commit()
    return {"drive": drive, **nodes}


@pytest.fixture
def janitor(tmp_path: Path, clock: FakeClock, repo_for_org: Any) -> Janitor:
    store = FilesystemStore(tmp_path / "bucket", clock=clock, layout="bucket")
    return Janitor(repo_for_org, AdminOnlyFactory(store), clock)


async def _lease(
    session: AsyncSession, org: Any, node_id: uuid.UUID, *, live: bool, ended_ago: int = 0
) -> None:
    await session.execute(
        text(
            "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
            "holder_kind, holder_principal_id, holder_instance_id, machine_id, purpose, "
            "expires_at, released_at) VALUES (:node, :org, 1, 'user', 'user', :holder, "
            "'i-1', 'ana-mbp', 'mount', now() + interval '10 minutes', "
            "CASE WHEN :live THEN NULL ELSE now() - make_interval(secs => :ago) END)"
        ),
        {
            "node": node_id,
            "org": org.org_team_id,
            "holder": org.admin_id,
            "live": live,
            "ago": ended_ago,
        },
    )
    await session.commit()


async def _flagged(janitor: Janitor, files_org: Any, world: dict[str, Any]) -> set[str]:
    report = await fsck_mod.run_fsck(
        janitor, org=files_org.scope, domain_id=DomainId(world["drive"].dedup_domain_id)
    )
    return {f.ref_id for f in report.by_code(fsck_mod.HOLDER_FACET_WITHOUT_LEASE)}


@pytest.mark.parametrize(
    ("lease", "flagged"),
    [
        pytest.param(None, {"held/deep/a.bin", "held/b.bin"}, id="no-lease-at-all"),
        pytest.param(("held", True, 0), set(), id="a-live-lease-on-the-folder"),
        pytest.param(("held/deep", True, 0), {"held/b.bin"}, id="a-live-lease-covering-one"),
        pytest.param(("held", False, GRACE - 60), set(), id="released-inside-the-grace"),
        pytest.param(
            ("held", False, GRACE + 60), {"held/deep/a.bin", "held/b.bin"}, id="released-past-it"
        ),
    ],
)
async def test_a_facet_with_no_lease_behind_it_is_named(
    janitor: Janitor,
    files_org: Any,
    files_session: AsyncSession,
    world: dict[str, Any],
    lease: tuple[str, bool, int] | None,
    flagged: set[str],
) -> None:
    # `other.bin` is outside every lease in every case, and always flagged.
    if lease is not None:
        folder, live, ago = lease
        await _lease(files_session, files_org, world[folder].id, live=live, ended_ago=ago)
    assert await _flagged(janitor, files_org, world) == {
        str(world[path].id) for path in {*flagged, "other.bin"}
    }


async def test_a_trashed_row_and_a_row_without_a_facet_are_not_named(
    janitor: Janitor, files_org: Any, files_session: AsyncSession, world: dict[str, Any]
) -> None:
    await files_session.execute(
        text("UPDATE file_nodes SET trashed_at = now() WHERE id = :n"),
        {"n": world["held/b.bin"].id},
    )
    await files_session.execute(
        text("UPDATE file_nodes SET holder_size = NULL WHERE id = :n"),
        {"n": world["other.bin"].id},
    )
    await files_session.commit()
    assert await _flagged(janitor, files_org, world) == {str(world["held/deep/a.bin"].id)}


async def test_the_finding_is_reported_not_repaired_and_not_quarantined(
    janitor: Janitor, files_org: Any, files_session: AsyncSession, world: dict[str, Any]
) -> None:
    report = await fsck_mod.run_fsck(
        janitor,
        org=files_org.scope,
        domain_id=DomainId(world["drive"].dedup_domain_id),
        repair_safe=True,
    )
    assert report.by_code(fsck_mod.HOLDER_FACET_WITHOUT_LEASE)
    assert not any(f.code == fsck_mod.HOLDER_FACET_WITHOUT_LEASE for f in report.repaired)
    quarantined = (
        await files_session.execute(
            text("SELECT count(*) FROM file_quarantine WHERE reason = :r"),
            {"r": fsck_mod.HOLDER_FACET_WITHOUT_LEASE},
        )
    ).scalar_one()
    facet = (
        await files_session.execute(
            text("SELECT holder_size FROM file_nodes WHERE id = :n"), {"n": world["other.bin"].id}
        )
    ).scalar_one()
    assert (quarantined, facet) == (0, 5)
