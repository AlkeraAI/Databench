"""The operator verbs, each over a deliberately planted fault.

Every case here seeds real rows on the lane database and real bytes in a
filesystem store under ``tmp_path``, then breaks exactly one thing and asserts
what the operator sees and what the process exits with. Nothing is mocked
except the store *handle* the CLI builds from settings, and that only so a
recording wrapper can prove ``--repair-safe`` never deletes.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from alkera_cli.commands import files_admin as admin
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.gc import DELETED_WINDOW
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store import FilesystemStore
from alkera_core.files.store.keys import DOMAIN_PREFIX, object_key
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.platform import FileQuarantine
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from typer.testing import CliRunner

#: Every seeded org gets its own bytes: the store key is content-addressed, so a
#: shared payload would give two orgs one key and ``inspect <key>`` would be
#: ambiguous by construction.
PAYLOAD_PREFIX = b"the bytes an operator is asking about" * 32


@dataclass(frozen=True, slots=True)
class Planted:
    """One org with one drive, one file node and its head version's bytes."""

    org_id: uuid.UUID
    domain_id: uuid.UUID
    node_id: uuid.UUID
    version_id: uuid.UUID
    store_key: str
    bucket: Path
    payload: bytes

    @property
    def object_path(self) -> Path:
        return self.bucket / DOMAIN_PREFIX / str(self.domain_id) / self.store_key


class RecordingStore:
    """A filesystem store that remembers every mutating call made through it.

    Only the mutations are recorded: the point of the log is the claim that
    ``fsck --repair-safe`` moves and deletes nothing, and a read tells you
    nothing about that.
    """

    def __init__(self, inner: FilesystemStore) -> None:
        self._inner = inner
        self.mutations: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def delete(self, key: str) -> None:
        self.mutations.append(("delete", key))
        await self._inner.delete(key)

    async def move(self, src: str, dst: str) -> None:
        self.mutations.append(("move", src))
        await self._inner.move(src, dst)


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich elides a narrow table's cells, and an elided id proves nothing."""
    monkeypatch.setenv("COLUMNS", "220")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def app() -> typer.Typer:
    """The sub-app under its own root, the way ``alkera files`` will mount it."""
    root = typer.Typer()
    root.add_typer(admin.admin_app)
    return root


class RecordingScoped:
    """A store factory whose admin handle is the recording store.

    The operator verbs are admin-rooted callers, so ``admin()`` is the only
    method they may reach for; ``for_domain`` raises rather than returning a
    handle, so a verb that started asking for a per-domain store would fail
    loudly here instead of quietly writing through an unrecorded path.
    """

    def __init__(self, recorder: RecordingStore) -> None:
        self._recorder = recorder

    def admin(self) -> Any:
        return self._recorder

    async def for_domain(self, domain_id: Any) -> Any:
        raise AssertionError("an operator verb must not hold a per-domain handle")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordingStore:
    """The store every verb in this module builds, rooted under ``tmp_path``."""
    bucket = tmp_path / "bucket"
    recorder = RecordingStore(FilesystemStore(bucket, clock=SystemClock(), layout="bucket"))
    monkeypatch.setattr(admin, "build_store_factory", lambda: RecordingScoped(recorder))
    return recorder


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _seed(bucket: Path) -> Planted:
    """A valid org → drive → node → version graph with its bytes on disk."""
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        org = Team(id=uuid.uuid4(), parent_team_id=None, name=f"admin-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        user = User(
            id=uuid.uuid4(),
            home_org_team_id=org.id,
            email=f"op-{uuid.uuid4().hex[:12]}@files.test",
            email_domain="files.test",
            first_name="Op",
            last_name="Erator",
        )
        session.add(user)
        await session.flush()
        session.add(TeamMembership(user_id=user.id, team_id=org.id, role=TeamRole.ADMIN))
        file_store = FileStore(
            id=uuid.uuid4(),
            driver="filesystem",
            bucket="",
            endpoint=str(bucket),
            region="",
            capabilities={},
            transfer_modes=["single", "proxied"],
        )
        session.add(file_store)
        await session.flush()
        domain = DedupDomain(id=uuid.uuid4(), org_team_id=org.id, region="", store_id=file_store.id)
        session.add(domain)
        await session.flush()
        drive = FileDrive(
            id=uuid.uuid4(),
            org_team_id=org.id,
            kind="org",
            store_id=file_store.id,
            dedup_domain_id=domain.id,
            quota_bytes=1 << 40,
            quota_nodes=1_000_000,
            next_ino=3,
        )
        session.add(drive)
        await session.flush()
        root = FileNode(
            id=uuid.uuid4(),
            ino=1,
            drive_id=drive.id,
            org_team_id=org.id,
            parent_id=None,
            kind="folder",
            name=b"",
            name_display="",
            name_key="",
            path_ids="0001",
            depth=0,
            traversal_only=True,
        )
        session.add(root)
        await session.flush()
        drive.root_node_id = root.id
        node = FileNode(
            id=uuid.uuid4(),
            ino=2,
            drive_id=drive.id,
            org_team_id=org.id,
            parent_id=root.id,
            kind="file",
            name=b"report.bin",
            name_display="report.bin",
            name_key="report.bin",
            path_ids="0001.0002",
            depth=1,
        )
        session.add(node)
        await session.flush()
        payload = PAYLOAD_PREFIX + node.id.bytes
        digests = hash_bytes(payload)
        key = object_key(digests.content_hash)
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=org.id,
            node_id=node.id,
            seq=1,
            size_bytes=len(payload),
            content_hash=digests.content_hash.hex(),
            store_key=key,
            source="upload",
        )
        session.add(version)
        await session.flush()
        node.head_version_id = version.id
        await session.commit()
        planted = Planted(
            org_id=org.id,
            domain_id=domain.id,
            node_id=node.id,
            version_id=version.id,
            store_key=key,
            bucket=bucket,
            payload=payload,
        )
    finally:
        await session.close()
        await engine.dispose()
    planted.object_path.parent.mkdir(parents=True, exist_ok=True)
    planted.object_path.write_bytes(planted.payload)
    return planted


@pytest.fixture
def no_sweep_shards() -> None:
    """A deployment on which nothing has ever swept: the shard table is empty.

    This is the state a freshly migrated database is in — the migrations create
    the table and insert no rows — so it is the state ``gc --dry-run`` has to
    survive on its own.
    """
    _run(_clear_shards())


async def _clear_shards() -> None:
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await session.execute(text("DELETE FROM file_sweep_shards"))
        await session.commit()
    finally:
        await session.close()
        await engine.dispose()


async def _shard_started_at(shard: int = 0) -> Any:
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        return (
            await session.execute(
                text("SELECT sweep_started_at FROM file_sweep_shards WHERE shard = :shard"),
                {"shard": shard},
            )
        ).scalar_one_or_none()
    finally:
        await session.close()
        await engine.dispose()


@pytest.fixture
def planted(tmp_path: Path, store: RecordingStore) -> Iterator[Planted]:
    yield _run(_seed(tmp_path / "bucket"))


async def _quarantine_rows(org_id: uuid.UUID) -> list[FileQuarantine]:
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT id, kind, ref_id, reason, attempts, resolved_at "
                    "FROM file_quarantine WHERE org_team_id = CAST(:org AS uuid)"
                ),
                {"org": str(org_id)},
            )
        ).all()
        return list(rows)  # type: ignore[arg-type]
    finally:
        await session.close()
        await engine.dispose()


async def _restore_generation() -> int:
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        value = (
            await session.execute(text("SELECT restore_generation FROM file_platform WHERE id = 1"))
        ).scalar_one()
        return int(value)
    finally:
        await session.close()
        await engine.dispose()


# -- verify -------------------------------------------------------------


def test_verify_matches_intact_bytes(runner: CliRunner, app: typer.Typer, planted: Planted) -> None:
    result = runner.invoke(app, ["admin", "verify", str(planted.node_id)])
    assert result.exit_code == admin.EXIT_OK, result.output
    assert "match" in result.output


def test_verify_reports_a_flipped_byte(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    corrupted = bytearray(planted.payload)
    corrupted[0] ^= 0xFF
    planted.object_path.write_bytes(bytes(corrupted))

    result = runner.invoke(app, ["admin", "verify", str(planted.node_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    assert "mismatch" in result.output


def test_verify_exits_usage_on_an_unknown_node(runner: CliRunner, app: typer.Typer) -> None:
    result = runner.invoke(app, ["admin", "verify", str(uuid.uuid4())])
    assert result.exit_code == admin.EXIT_USAGE, result.output


# -- scrub --------------------------------------------------------------


def test_scrub_is_clean_over_intact_bytes(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    result = runner.invoke(app, ["admin", "scrub", "--full", "--org", str(planted.org_id)])
    assert result.exit_code == admin.EXIT_OK, result.output
    assert not _run(_quarantine_rows(planted.org_id))


def test_scrub_quarantines_a_flipped_byte(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    corrupted = bytearray(planted.payload)
    corrupted[-1] ^= 0x01
    planted.object_path.write_bytes(bytes(corrupted))

    result = runner.invoke(app, ["admin", "scrub", "--full", "--org", str(planted.org_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    rows = _run(_quarantine_rows(planted.org_id))
    assert [(row.kind, row.ref_id) for row in rows] == [("version", str(planted.version_id))]
    assert rows[0].reason == "scrub.content_hash_mismatch"
    # The scrub never repairs: the bytes it disagreed with are still there.
    assert planted.object_path.read_bytes() == bytes(corrupted)


def test_scrub_refuses_an_out_of_range_sample(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    result = runner.invoke(app, ["admin", "scrub", "--sample", "101"])
    assert result.exit_code == admin.EXIT_USAGE, result.output


def test_scrub_refuses_a_malformed_org(runner: CliRunner, app: typer.Typer) -> None:
    result = runner.invoke(app, ["admin", "scrub", "--org", "not-a-uuid"])
    assert result.exit_code == admin.EXIT_USAGE, result.output


# -- fsck ---------------------------------------------------------------


def test_fsck_finds_an_orphan_object(
    runner: CliRunner, app: typer.Typer, planted: Planted, store: RecordingStore
) -> None:
    orphan = planted.object_path.parent / ("0" * 64)
    orphan.write_bytes(b"nobody references these")

    result = runner.invoke(app, ["admin", "fsck", "--org", str(planted.org_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    assert "fsck.orphan_object" in result.output.replace("\n", "")


def test_fsck_finds_a_parked_object_past_the_deleted_window(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    """An object parked under ``deleted/`` long enough is a finding.

    It only can be if the janitor the verb builds has an object-age source:
    with none, ``written_at`` answers ``None`` for every key and the window
    check can never fire, so the operator's only fsck reports nothing however
    long the bytes have sat there.
    """
    parked = planted.bucket / DOMAIN_PREFIX / str(planted.domain_id) / "deleted" / ("2" * 64)
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_bytes(b"parked for the window to pass")
    long_ago = (datetime.now(tz=UTC) - DELETED_WINDOW - timedelta(days=1)).timestamp()
    os.utime(parked, (long_ago, long_ago))

    result = runner.invoke(app, ["admin", "fsck", "--org", str(planted.org_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    assert "fsck.deleted_past_window" in result.output.replace("\n", "")


def test_fsck_repair_safe_never_deletes(
    runner: CliRunner, app: typer.Typer, planted: Planted, store: RecordingStore
) -> None:
    orphan = planted.object_path.parent / ("1" * 64)
    orphan.write_bytes(b"an orphan is reported, never removed")

    result = runner.invoke(app, ["admin", "fsck", "--repair-safe", "--org", str(planted.org_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    assert store.mutations == []
    assert orphan.exists()


# -- gc -----------------------------------------------------------------


def test_gc_dry_run_prints_a_plan_and_moves_nothing(
    runner: CliRunner,
    app: typer.Typer,
    planted: Planted,
    store: RecordingStore,
) -> None:
    result = runner.invoke(app, ["admin", "gc", "--dry-run", "--org", str(planted.org_id)])
    assert result.exit_code == admin.EXIT_OK, result.output
    assert "files gc" in result.output
    assert store.mutations == []


def test_gc_dry_run_plans_on_a_freshly_migrated_database(
    runner: CliRunner,
    app: typer.Typer,
    planted: Planted,
    store: RecordingStore,
    no_sweep_shards: None,
) -> None:
    """A first sweep is what brings shard 0 into being, so ``gc`` seeds it.

    Nothing else in the deployment inserts the row: without the seed the very
    first ``gc --dry-run`` an operator runs dies on a missing shard.
    """
    assert _run(_shard_started_at()) is None

    result = runner.invoke(app, ["admin", "gc", "--dry-run", "--org", str(planted.org_id)])

    assert result.exit_code == admin.EXIT_OK, result.output
    # The row exists *and* carries the horizon the plan stamped on it: an
    # inserted-but-unstamped row would still have failed the planner.
    assert _run(_shard_started_at()) is not None
    assert store.mutations == []


def test_gc_refuses_to_sweep_for_real(
    runner: CliRunner, app: typer.Typer, planted: Planted, store: RecordingStore
) -> None:
    result = runner.invoke(app, ["admin", "gc", "--no-dry-run", "--org", str(planted.org_id)])
    assert result.exit_code == admin.EXIT_USAGE, result.output
    assert store.mutations == []


# -- quarantine ---------------------------------------------------------


def test_quarantine_list_retry_and_discard(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    corrupted = bytearray(planted.payload)
    corrupted[3] ^= 0x40
    planted.object_path.write_bytes(bytes(corrupted))
    runner.invoke(app, ["admin", "scrub", "--full", "--org", str(planted.org_id)])
    entry = _run(_quarantine_rows(planted.org_id))[0]

    listed = runner.invoke(app, ["admin", "quarantine", "list", "--org", str(planted.org_id)])
    assert listed.exit_code == admin.EXIT_FINDINGS, listed.output
    assert str(planted.version_id) in listed.output.replace("\n", "")

    retried = runner.invoke(app, ["admin", "quarantine", "retry", str(entry.id)])
    assert retried.exit_code == admin.EXIT_OK, retried.output
    assert _run(_quarantine_rows(planted.org_id))[0].attempts == entry.attempts + 1

    discarded = runner.invoke(app, ["admin", "quarantine", "discard", str(entry.id)])
    assert discarded.exit_code == admin.EXIT_OK, discarded.output
    assert _run(_quarantine_rows(planted.org_id))[0].resolved_at is not None

    after = runner.invoke(app, ["admin", "quarantine", "list", "--org", str(planted.org_id)])
    assert after.exit_code == admin.EXIT_OK, after.output


def test_quarantine_retry_of_an_unknown_entry_exits_usage(
    runner: CliRunner, app: typer.Typer, store: RecordingStore
) -> None:
    result = runner.invoke(app, ["admin", "quarantine", "retry", str(uuid.uuid4())])
    assert result.exit_code == admin.EXIT_USAGE, result.output


# -- restore-drill ------------------------------------------------------


def test_restore_drill_dry_run_leaves_file_platform_untouched(
    runner: CliRunner, app: typer.Typer, store: RecordingStore
) -> None:
    before = _run(_restore_generation())

    result = runner.invoke(app, ["admin", "restore-drill"])

    assert result.exit_code == admin.EXIT_OK, result.output
    assert str(before + 1) in result.output.replace("\n", "")
    assert _run(_restore_generation()) == before


def test_restore_drill_apply_bumps_the_generation(
    runner: CliRunner, app: typer.Typer, store: RecordingStore
) -> None:
    before = _run(_restore_generation())

    result = runner.invoke(app, ["admin", "restore-drill", "--apply"])

    assert result.exit_code == admin.EXIT_OK, result.output
    assert _run(_restore_generation()) == before + 1


# -- inspect ------------------------------------------------------------


@pytest.mark.parametrize("by", ["node", "version", "key"])
def test_inspect_resolves_every_reference_form(
    runner: CliRunner, app: typer.Typer, planted: Planted, by: str
) -> None:
    ref = {
        "node": str(planted.node_id),
        "version": str(planted.version_id),
        "key": planted.store_key,
    }[by]

    result = runner.invoke(app, ["admin", "inspect", ref])

    assert result.exit_code == admin.EXIT_OK, result.output
    flat = result.output.replace("\n", "")
    assert planted.store_key.split("/")[-1][:16] in flat
    assert str(len(planted.payload)) in flat


def test_inspect_reports_a_missing_object(
    runner: CliRunner, app: typer.Typer, planted: Planted
) -> None:
    planted.object_path.unlink()

    result = runner.invoke(app, ["admin", "inspect", str(planted.version_id)])

    assert result.exit_code == admin.EXIT_FINDINGS, result.output
    assert "missing" in result.output


def test_inspect_exits_usage_on_an_unknown_reference(
    runner: CliRunner, app: typer.Typer, store: RecordingStore
) -> None:
    result = runner.invoke(app, ["admin", "inspect", "objects/ff/ee/" + "f" * 64])
    assert result.exit_code == admin.EXIT_USAGE, result.output
