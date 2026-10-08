"""Plant a fault, and prove exactly that fault is what comes back.

Every test here writes real bytes into a real filesystem store and real rows
into real Postgres, breaks exactly one of the two, and asserts the code fsck or
scrub names. The point of the shape is that a check which quietly reports
everything, or nothing, fails: a clean domain must report nothing, and each
planted fault must produce its own code and no other.

The repair rule is asserted from both sides — the safe subset is fixed, the
rest is left exactly as it was, and a store call log proves no delete happened
on any path.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files import fsck as fsck_mod
from alkera_core.files import scrub as scrub_mod
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store.keys import object_key
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


class RecordingStore:
    """The admin handle, with a call log so 'never deletes' is checkable."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []
        self.capabilities = inner.capabilities

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def head(self, key: str) -> Any:
        self.calls.append(("head", key))
        return await self._inner.head(key)

    async def get(self, key: str, **kwargs: Any) -> Any:
        self.calls.append(("get", key))
        return await self._inner.get(key, **kwargs)

    async def list_prefix(self, prefix: str, **kwargs: Any) -> Any:
        self.calls.append(("list_prefix", prefix))
        return await self._inner.list_prefix(prefix, **kwargs)

    async def delete(self, key: str) -> None:
        self.calls.append(("delete", key))
        await self._inner.delete(key)

    async def move(self, src: str, dst: str) -> None:
        self.calls.append(("move", src))
        await self._inner.move(src, dst)


@pytest.fixture
async def drive(files_factory: Any) -> Any:
    """The drive under test, created before the store so the two agree.

    The domain is the drive's own ``dedup_domain_id`` rather than a fresh uuid:
    fsck resolves versions through ``file_drives``, so a store rooted at any
    other domain would find an empty world and report a clean domain for the
    wrong reason.
    """
    return await files_factory.drive()


@pytest.fixture
def domain(tmp_path: Path, clock: FakeClock, drive: Any) -> Any:
    from alkera_core.files.ids import DomainId
    from alkera_core.files.store.filesystem import FilesystemStore
    from tests.files.gc.conftest import Domain, MtimeAges

    root = tmp_path / "bucket"
    store = FilesystemStore(root, clock=clock, layout="bucket")
    return Domain(id=DomainId(drive.dedup_domain_id), root=root, store=store, ages=MtimeAges(root))


@pytest.fixture
def recording(domain: Any) -> RecordingStore:
    return RecordingStore(domain.store)


@pytest.fixture
def janitor(recording: RecordingStore, repo_for_org: Any, clock: FakeClock, domain: Any) -> Janitor:
    return Janitor(repo_for_org, AdminOnlyFactory(recording), clock, age_source=domain.ages)


class World:
    """A domain, a drive inside it, and one file whose bytes really are there."""

    def __init__(self, seeder: Any, node: FileNode, folder: FileNode, payload: bytes) -> None:
        self.seeder = seeder
        self.node = node
        self.folder = folder
        self.payload = payload
        self.key = object_key(hash_bytes(payload).content_hash)


@pytest.fixture
async def world(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
    domain: Any,
    drive: Any,
) -> World:
    """One consistent domain: bytes on the store, a version and a head that agree."""
    tree = await files_factory.tree("box/ box/doc.bin", drive=drive)
    folder, node = tree["box"], tree["box/doc.bin"]

    payload = b"the quick brown fox" * 7
    digests = hash_bytes(payload)
    key = object_key(digests.content_hash)
    domain.write(key, payload)

    from tests.files.gc.conftest import Seeder

    seeder = Seeder(files_session, files_org, drive)
    version = await seeder.version(node, key, size=len(payload))
    version.content_hash = digests.content_hash.hex()
    node.head_version_id = version.id
    files_session.add(_dir_stats(files_org.org_team_id, folder.id, len(payload), files=1, kids=1))
    await files_session.commit()
    return World(seeder, node, folder, payload)


def _dir_stats(org: uuid.UUID, node_id: uuid.UUID, size: int, *, files: int, kids: int) -> Any:
    from alkera_core.models.files.history import FileDirStats

    return FileDirStats(
        node_id=node_id, org_team_id=org, bytes=size, files=files, direct_children=kids
    )


async def _report(janitor: Janitor, files_org: Any, domain: Any, **kwargs: Any) -> Any:
    return await fsck_mod.run_fsck(janitor, org=files_org.scope, domain_id=domain.id, **kwargs)


async def _quarantine_rows(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.execute(text("SELECT reason, ref_id FROM file_quarantine"))
    return [(row[0], row[1]) for row in rows]


# -- a clean domain reports nothing --------------------------------------


async def test_a_consistent_domain_reports_no_finding(
    janitor: Janitor, world: World, files_org: Any, domain: Any
) -> None:
    report = await _report(janitor, files_org, domain)
    assert report.codes == ()
    assert report.clean


async def test_a_consistent_domain_scrubs_clean(
    janitor: Janitor, world: World, files_org: Any, domain: Any
) -> None:
    result = await scrub_mod.run_scrub(janitor, org=files_org.scope, domain_id=domain.id)
    assert result.findings == ()
    assert result.sampled == 1
    assert result.bytes_read == len(world.payload)


# -- one planted fault at a time -----------------------------------------


async def test_scrub_finds_a_flipped_byte(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    files_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A byte flipped under the store, with the size left alone, is caught."""
    corrupted = bytearray(world.payload)
    corrupted[3] ^= 0xFF
    domain.write(world.key, bytes(corrupted))

    result = await scrub_mod.run_scrub(janitor, org=files_org.scope, domain_id=domain.id)

    assert [f.reason for f in result.findings] == [scrub_mod.MISMATCH]
    assert result.findings[0].store_key == world.key
    assert (scrub_mod.MISMATCH, str(world.node.head_version_id)) in await _quarantine_rows(
        files_session
    )
    # never repairs: the bytes on disk are the corrupt ones the scrub read
    assert (domain.root / "domains" / str(domain.id) / world.key).read_bytes() == bytes(corrupted)


async def test_scrub_finds_a_size_change_without_reading_the_object(
    janitor: Janitor, world: World, files_org: Any, domain: Any, recording: RecordingStore
) -> None:
    """A truncation is a head, not a read: the size disagrees before any bytes."""
    domain.write(world.key, world.payload[:5])
    result = await scrub_mod.run_scrub(janitor, org=files_org.scope, domain_id=domain.id)
    assert [f.reason for f in result.findings] == [scrub_mod.SIZE_MISMATCH]
    assert not any(call[0] == "get" for call in recording.calls)


async def test_fsck_finds_a_dangling_reference_and_quarantines_the_version(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    (domain.root / "domains" / str(domain.id) / world.key).unlink()
    report = await _report(janitor, files_org, domain)
    assert report.by_code(fsck_mod.DANGLING_REFERENCE)
    assert (
        fsck_mod.DANGLING_REFERENCE,
        str(world.node.head_version_id),
    ) in await _quarantine_rows(files_session)


async def test_fsck_finds_an_orphan_object(
    janitor: Janitor, world: World, files_org: Any, domain: Any
) -> None:
    stray = object_key(hashlib.sha256(b"nobody references me").digest())
    domain.write(stray, b"nobody references me")
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.ORPHAN_OBJECT)] == [stray]


async def test_fsck_finds_a_head_pointing_at_another_nodes_version(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    files_session: AsyncSession,
    files_factory: Any,
) -> None:
    other = await world.seeder.version(world.folder, world.key, size=len(world.payload), seq=9)
    world.node.head_version_id = other.id
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.BAD_HEAD_POINTER)] == [str(world.node.id)]


async def test_fsck_finds_a_version_addressing_another_domains_prefix(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    await files_session.execute(
        text("UPDATE file_versions SET store_key = :key WHERE id = :id"),
        {"key": f"domains/{uuid.uuid4()}/{world.key}", "id": world.node.head_version_id},
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert report.by_code(fsck_mod.OBJECT_OUTSIDE_PREFIX)
    # and it is reported as *that*, not as a dangling reference
    assert not report.by_code(fsck_mod.DANGLING_REFERENCE)


async def test_fsck_finds_dir_stats_drift(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    await files_session.execute(
        text("UPDATE file_dir_stats SET bytes = bytes + 4096, files = 77 WHERE node_id = :n"),
        {"n": world.folder.id},
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    drift = report.by_code(fsck_mod.DIR_STATS_DRIFT)
    assert [f.ref_id for f in drift] == [str(world.folder.id)]
    assert drift[0].detail["recomputed"] == [len(world.payload), 1, 1]


async def test_fsck_finds_an_inline_size_mismatch(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    await files_session.execute(
        text(
            "UPDATE file_versions SET store_key = NULL, inline_bytes = :b, size_bytes = 99 "
            "WHERE id = :id"
        ),
        {"b": b"short", "id": world.node.head_version_id},
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    finding = report.by_code(fsck_mod.INLINE_SIZE_MISMATCH)
    assert len(finding) == 1
    assert finding[0].detail == {"declared": 99, "stored": 5}


# -- the stuck states ----------------------------------------------------


async def test_fsck_finds_a_committing_session_older_than_an_hour(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    session_row = await world.seeder.upload_session(world.folder, state="committing")
    await files_session.execute(
        text("UPDATE file_upload_sessions SET created_at = now() - interval '2 hours' WHERE id=:i"),
        {"i": session_row.id},
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert str(session_row.id) in [f.ref_id for f in report.by_code(fsck_mod.COMMITTING_STALE)]


async def test_fsck_finds_a_hold_that_does_not_match_its_open_session(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    """Σholds == Σopen: a live session's hold must equal the bytes it declared."""
    session_row = await world.seeder.upload_session(world.folder, state="open")
    await files_session.execute(
        text(
            "UPDATE file_upload_sessions SET declared_size = 1000, quota_hold_bytes = 0 "
            "WHERE id = :i"
        ),
        {"i": session_row.id},
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.HOLD_SUM_MISMATCH)] == [str(session_row.id)]


@pytest.mark.parametrize(
    ("state", "repairable"),
    [
        pytest.param("acl_rewriting", True, id="acl_rewriting-is-safe-to-clear"),
        pytest.param("moving", False, id="moving-is-never-cleared"),
    ],
)
async def test_fsck_finds_a_flagged_node_with_no_live_operation(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    files_session: AsyncSession,
    state: str,
    repairable: bool,
) -> None:
    await files_session.execute(
        text(
            "UPDATE file_nodes SET state = :s, updated_at = now() - interval '1 hour' WHERE id=:i"
        ),
        {"s": state, "i": world.folder.id},
    )
    await files_session.commit()

    report = await _report(janitor, files_org, domain, repair_safe=True)
    assert [f.ref_id for f in report.by_code(fsck_mod.NODE_FLAG_WITHOUT_OP)] == [
        str(world.folder.id)
    ]
    after = (
        await files_session.execute(
            text("SELECT state FROM file_nodes WHERE id = :i"), {"i": world.folder.id}
        )
    ).scalar_one()
    assert (after == "live") is repairable


async def test_a_live_operation_keeps_a_flagged_node_out_of_the_report(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    """The negative twin: the flag is only a fault when nothing is driving it."""
    await files_session.execute(
        text(
            "UPDATE file_nodes SET state='moving', updated_at = now() - interval '1 hour' "
            "WHERE id = :i"
        ),
        {"i": world.folder.id},
    )
    files_session.add(
        FileOp(
            id=uuid.uuid4(),
            org_team_id=world.folder.org_team_id,
            drive_id=world.folder.drive_id,
            kind="move",
            actor=uuid.uuid4(),
            state="running",
            heartbeat_at=datetime.now(UTC),
        )
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert report.by_code(fsck_mod.NODE_FLAG_WITHOUT_OP) == ()


async def test_fsck_finds_a_trashed_node_past_its_purge_deadline(
    janitor: Janitor, world: World, files_org: Any, domain: Any, clock: FakeClock
) -> None:
    await world.seeder.trash(world.node, datetime.now(UTC) - timedelta(days=3))
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.TRASHED_PAST_PURGE)] == [str(world.node.id)]


async def test_fsck_finds_a_running_operation_with_no_heartbeat(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    op = FileOp(
        id=uuid.uuid4(),
        org_team_id=world.node.org_team_id,
        drive_id=world.node.drive_id,
        kind="move",
        actor=uuid.uuid4(),
        state="running",
        heartbeat_at=None,
    )
    files_session.add(op)
    await files_session.commit()
    report = await _report(janitor, files_org, domain)
    assert str(op.id) in [f.ref_id for f in report.by_code(fsck_mod.OP_WITHOUT_HEARTBEAT)]


async def test_fsck_finds_staged_bytes_whose_session_is_gone(
    janitor: Janitor, world: World, files_org: Any, domain: Any
) -> None:
    dead = uuid.uuid4()
    domain.write(f"incoming/{dead}/object", b"staged by nobody")
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.INCOMING_PAST_TTL)] == [
        f"incoming/{dead}/object"
    ]


async def test_a_live_session_keeps_its_staged_bytes_out_of_the_report(
    janitor: Janitor, world: World, files_org: Any, domain: Any
) -> None:
    live = await world.seeder.upload_session(world.folder, state="uploading")
    domain.write(f"incoming/{live.id}/object", b"staged by a live session")
    report = await _report(janitor, files_org, domain)
    assert report.by_code(fsck_mod.INCOMING_PAST_TTL) == ()


async def test_fsck_finds_an_object_parked_under_deleted_past_the_window(
    janitor: Janitor, world: World, files_org: Any, domain: Any, clock: FakeClock
) -> None:
    domain.write(f"deleted/{world.key}", world.payload)
    domain.ages.set(f"domains/{domain.id}/deleted/{world.key}", clock.now() - timedelta(days=30))
    report = await _report(janitor, files_org, domain)
    assert [f.ref_id for f in report.by_code(fsck_mod.DELETED_PAST_WINDOW)] == [
        f"deleted/{world.key}"
    ]


# -- repair-safe: fixes the safe subset, deletes nothing ------------------


async def test_repair_safe_recomputes_dir_stats_and_leaves_the_rest(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    files_session: AsyncSession,
    recording: RecordingStore,
) -> None:
    """Both faults are found; only the safe one changes anything."""
    await files_session.execute(
        text(
            "UPDATE file_dir_stats SET bytes = 1, files = 0, direct_children = 0 WHERE node_id=:n"
        ),
        {"n": world.folder.id},
    )
    await files_session.commit()
    stray = object_key(hashlib.sha256(b"orphan").digest())
    domain.write(stray, b"orphan")

    report = await _report(janitor, files_org, domain, repair_safe=True)

    assert set(report.codes) == {fsck_mod.DIR_STATS_DRIFT, fsck_mod.ORPHAN_OBJECT}
    assert [f.code for f in report.repaired] == [fsck_mod.DIR_STATS_DRIFT]
    row = (
        await files_session.execute(
            text("SELECT bytes, files, direct_children FROM file_dir_stats WHERE node_id = :n"),
            {"n": world.folder.id},
        )
    ).one()
    assert tuple(row) == (len(world.payload), 1, 1)
    # the unsafe one is untouched on both planes
    assert domain.exists(stray)
    assert not [call for call in recording.calls if call[0] in ("delete", "move")]


async def test_repair_safe_requeues_a_stuck_committing_session(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    session_row = await world.seeder.upload_session(world.folder, state="committing")
    await files_session.execute(
        text(
            "UPDATE file_upload_sessions SET created_at = now() - interval '3 hours', "
            "declared_size = 0, quota_hold_bytes = 0 WHERE id = :i"
        ),
        {"i": session_row.id},
    )
    await files_session.commit()

    report = await _report(janitor, files_org, domain, repair_safe=True)

    assert fsck_mod.COMMITTING_STALE in [f.code for f in report.repaired]
    state = (
        await files_session.execute(
            text("SELECT state FROM file_upload_sessions WHERE id = :i"), {"i": session_row.id}
        )
    ).scalar_one()
    assert state == "uploading"


async def test_without_repair_safe_nothing_is_fixed(
    janitor: Janitor, world: World, files_org: Any, domain: Any, files_session: AsyncSession
) -> None:
    """The negative twin of every repair: the flag alone decides."""
    await files_session.execute(
        text("UPDATE file_dir_stats SET bytes = 1 WHERE node_id = :n"), {"n": world.folder.id}
    )
    await files_session.commit()
    report = await _report(janitor, files_org, domain, repair_safe=False)
    assert report.by_code(fsck_mod.DIR_STATS_DRIFT)
    assert report.repaired == ()
    cached = (
        await files_session.execute(
            text("SELECT bytes FROM file_dir_stats WHERE node_id = :n"), {"n": world.folder.id}
        )
    ).scalar_one()
    assert cached == 1


# -- the scrub's budget and cursor ---------------------------------------


@pytest.mark.parametrize(
    ("sample_pct", "expected"),
    [
        pytest.param(0, 0, id="0pct-samples-nothing"),
        pytest.param(100, 1, id="100pct-samples-everything"),
    ],
)
async def test_scrub_honours_the_sample_percentage(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    sample_pct: int,
    expected: int,
) -> None:
    result = await scrub_mod.run_scrub(
        janitor, org=files_org.scope, domain_id=domain.id, sample_pct=sample_pct
    )
    assert result.sampled == expected


async def test_scrub_stops_on_its_budget_and_resumes_from_its_cursor(
    janitor: Janitor,
    world: World,
    files_org: Any,
    domain: Any,
    files_session: AsyncSession,
) -> None:
    """Two budgeted runs cover what one unbudgeted run would, and no more."""
    payloads = [b"second object bytes", b"third object bytes!!"]
    for seq, payload in enumerate(payloads, start=5):
        digests = hash_bytes(payload)
        key = object_key(digests.content_hash)
        domain.write(key, payload)
        version = await world.seeder.version(world.node, key, size=len(payload), seq=seq)
        version.content_hash = digests.content_hash.hex()
    await files_session.commit()

    first = await scrub_mod.run_scrub(
        janitor, org=files_org.scope, domain_id=domain.id, budget_bytes=1
    )
    assert first.sampled == 1
    assert not first.exhausted
    assert first.cursor is not None

    second = await scrub_mod.run_scrub(
        janitor, org=files_org.scope, domain_id=domain.id, cursor=first.cursor
    )
    assert second.exhausted
    assert second.sampled == 2
    assert first.findings == () and second.findings == ()
