"""The Files runtime models against real Postgres.

The second Appendix A3 group is where the operational invariants live, so the
tests are not only round-trips: each one makes the database refuse a write it
must refuse. One live lease per node, one row per part number, one idempotency
record per (org, key, principal, route) — all three are primary keys, and a
test that only inserted valid rows would not notice if one of them were
widened.

The round-trips still matter for fidelity: a session's filename is bytes that
need not be UTF-8, a stage cursor is JSONB that must not flatten to text, and a
chunk's HMAC hash is raw bytes that must come back byte-identical.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from alkera_core.db.base import Base
from alkera_core.models.files import (
    FILES_CHURN_TABLES,
    DedupDomain,
    FileAcl,
    FileDrive,
    FileErasureLog,
    FileHold,
    FileIdempotencyKey,
    FileKeyChunk,
    FileLease,
    FileLeaseEpochHwm,
    FileLink,
    FileLock,
    FileManifest,
    FileNode,
    FileOp,
    FilePack,
    FilePlatform,
    FileQuarantine,
    FileStageJob,
    FileStore,
    FileSweepShard,
    FileUploadPart,
    FileUploadSession,
    ManifestTerm,
    StorageUsageSnapshot,
)
from sqlalchemy import Engine, Index, create_engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = pytest.mark.xdist_group("files_models_runtime")

#: The models this lane owns, in the order a reader wants to meet them.
MODELS = (
    FileOp,
    FileIdempotencyKey,
    FileUploadSession,
    FileUploadPart,
    FileLease,
    FileLeaseEpochHwm,
    FileStageJob,
    FilePlatform,
    FileSweepShard,
    FileQuarantine,
    FileLink,
    FileLock,
    FileHold,
    FileManifest,
    ManifestTerm,
    FilePack,
    FileKeyChunk,
    StorageUsageSnapshot,
    FileErasureLog,
)
OWNED_TABLES = tuple(model.__table__ for model in MODELS)

#: Every Files table, including the first group the foreign keys point at:
#: the migration has to have created all of them for this module to run.
ALL_FILES_TABLES = [
    table
    for table in Base.metadata.sorted_tables
    if table.name.startswith("file_")
    or table.name in {"dedup_domains", "manifest_terms", "storage_usage_snapshots"}
]

#: A filename UTF-8 cannot decode: the exact thing the bytea ``name`` exists for.
UNDECODABLE_NAME = b"pl\xe4n\xff.bin"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """The migrated schema, exactly as it is — never one this module builds.

    ``create_all`` from the model metadata produces a *different* schema: it
    carries no trigger, no row-level security, no seed row and none of the
    hand-written storage parameters, so a round trip against it would prove the
    models agree with themselves rather than with what a deployment runs. Worse,
    the matching ``drop_all`` deletes the migrated tables out from under the rest
    of the session. So the fixture only asserts the tables are there and leaves
    the schema alone.
    """
    dsn = os.environ.get("DATABASE_URL_SYNC")
    if not dsn:
        pytest.skip("DATABASE_URL_SYNC is unset")
    eng = create_engine(dsn)
    missing = sorted(
        table.name for table in ALL_FILES_TABLES if not inspect(eng).has_table(table.name)
    )
    if missing:
        eng.dispose()
        pytest.skip(f"the Files migrations are not applied to DATABASE_URL_SYNC: {missing}")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on its own transaction, rolled back so tests cannot see each
    other's rows."""
    connection = engine.connect()
    transaction = connection.begin()
    with Session(bind=connection) as db:
        yield db
    if transaction.is_active:
        transaction.rollback()
    connection.close()


class Tree:
    """The minimal chain every row in this group hangs from."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.org_team_id = uuid.uuid4()
        self.store = FileStore(
            driver="s3",
            bucket="alkera-files",
            endpoint="https://s3.example.invalid",
            region="us-west-2",
            capabilities={"conditional_write": True},
            transfer_modes=["single", "proxied"],
        )
        db.add(self.store)
        db.flush()
        self.domain = DedupDomain(
            org_team_id=self.org_team_id,
            region="us-west-2",
            store_id=self.store.id,
            chunker_seed=b"\x00\xff",
            hmac_key_id="hmac-1",
        )
        db.add(self.domain)
        db.flush()
        self.drive = FileDrive(
            org_team_id=self.org_team_id,
            kind="org",
            store_id=self.store.id,
            dedup_domain_id=self.domain.id,
            quota_bytes=1 << 40,
            quota_nodes=1000,
            next_ino=10,
        )
        db.add(self.drive)
        db.flush()
        self.acl = FileAcl(
            org_team_id=self.org_team_id,
            body=[{"principal_kind": "org", "principal_id": str(self.org_team_id), "role": "read"}],
            body_hash=uuid.uuid4().hex * 2,
        )
        db.add(self.acl)
        db.flush()
        self.root = self.node(b"", parent_id=None, ino=1, kind="folder")
        self.drive.root_node_id = self.root.id
        db.flush()

    def node(
        self,
        name: bytes,
        *,
        parent_id: uuid.UUID | None,
        ino: int,
        kind: str = "file",
    ) -> FileNode:
        node = FileNode(
            ino=ino,
            drive_id=self.drive.id,
            org_team_id=self.org_team_id,
            parent_id=parent_id,
            kind=kind,
            name=name,
            name_display=name.decode("utf-8", "replace"),
            name_key=name.decode("utf-8", "replace").casefold(),
            path_ids=f"n{uuid.uuid4().hex}",
            depth=0 if parent_id is None else 1,
            acl_id=self.acl.id,
        )
        self.db.add(node)
        self.db.flush()
        return node

    def upload_session(self, **overrides: object) -> FileUploadSession:
        kwargs: dict[str, object] = {
            "org_team_id": self.org_team_id,
            "drive_id": self.drive.id,
            "parent_id": self.root.id,
            "name": UNDECODABLE_NAME,
            "dedup_domain_id": self.domain.id,
            "state": "uploading",
            "declared_size": 4096,
            "bytes_received": 1024,
            "store_upload_id": "mpu-1",
            "store_key": "incoming/abc",
            "transfer_mode": "proxied",
            "lease_epoch": 7,
            "quota_hold_bytes": 4096,
            "quota_hold_nodes": 1,
            "expires_at": EPOCH + timedelta(days=7),
            "created_by": uuid.uuid4(),
        }
        kwargs.update(overrides)
        row = FileUploadSession(**kwargs)
        self.db.add(row)
        self.db.flush()
        return row


@pytest.fixture
def tree(session: Session) -> Tree:
    return Tree(session)


def test_file_op_round_trips_its_inverse_and_result(session: Session, tree: Tree) -> None:
    """An operation's inverse and result are JSONB structures, not strings."""
    target = tree.node(b"moved.txt", parent_id=tree.root.id, ino=11)
    op = FileOp(
        org_team_id=tree.org_team_id,
        drive_id=tree.drive.id,
        kind="move",
        actor=uuid.uuid4(),
        idempotency_key="key-move-1",
        inverse={"kind": "move", "to": str(tree.root.id), "nodes": [str(target.id)]},
        undoable_until=EPOCH + timedelta(days=30),
        state="running",
        heartbeat_at=EPOCH,
        progress={"done": 3, "total": 10},
        errors=[{"code": "files.conflict", "node": str(target.id)}],
        conflicts=[str(target.id)],
        result_node_id=target.id,
        result={"resultUrl": "https://content.example.invalid/x"},
    )
    session.add(op)
    session.flush()
    session.expire_all()

    read = session.get(FileOp, op.id)
    assert read is not None
    assert read.inverse == {"kind": "move", "to": str(tree.root.id), "nodes": [str(target.id)]}
    assert read.progress == {"done": 3, "total": 10}
    assert read.errors == [{"code": "files.conflict", "node": str(target.id)}]
    assert read.conflicts == [str(target.id)]
    assert read.result == {"resultUrl": "https://content.example.invalid/x"}
    assert read.result_node_id == target.id
    assert read.state == "running"


def test_file_op_idempotency_key_is_unique_per_org_but_optional(
    session: Session, tree: Tree
) -> None:
    """Two operations may both carry no key; two may not carry the same one."""
    for _ in range(2):
        session.add(
            FileOp(
                org_team_id=tree.org_team_id,
                drive_id=tree.drive.id,
                kind="copy",
                actor=uuid.uuid4(),
                idempotency_key=None,
            )
        )
    session.flush()

    session.add(
        FileOp(
            org_team_id=tree.org_team_id,
            drive_id=tree.drive.id,
            kind="copy",
            actor=uuid.uuid4(),
            idempotency_key="key-copy-1",
        )
    )
    session.flush()
    session.add(
        FileOp(
            org_team_id=tree.org_team_id,
            drive_id=tree.drive.id,
            kind="copy",
            actor=uuid.uuid4(),
            idempotency_key="key-copy-1",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def _idempotency_row(tree: Tree, **overrides: object) -> FileIdempotencyKey:
    kwargs: dict[str, object] = {
        "org_team_id": tree.org_team_id,
        "key": "idem-1",
        "principal_id": uuid.UUID(int=1),
        "route": "POST /api/v1/files/upload",
        "request_hash": "a" * 64,
        "status": "succeeded",
        "body": {"id": "node-1", "name": "report.pdf"},
    }
    kwargs.update(overrides)
    return FileIdempotencyKey(**kwargs)


def test_idempotency_key_round_trips_the_stored_body(session: Session, tree: Tree) -> None:
    row = _idempotency_row(tree)
    session.add(row)
    session.flush()
    session.expire_all()

    read = session.get(
        FileIdempotencyKey,
        (tree.org_team_id, "idem-1", uuid.UUID(int=1), "POST /api/v1/files/upload"),
    )
    assert read is not None
    assert read.body == {"id": "node-1", "name": "report.pdf"}
    assert read.status == "succeeded"
    assert read.request_hash == "a" * 64


def test_idempotency_key_refuses_a_replay_but_allows_another_route(
    session: Session, tree: Tree
) -> None:
    """The same key on the same route by the same principal is one record —
    that is what makes ``conflict=rename`` rename once. On another route it is
    a different request and must be allowed."""
    session.add(_idempotency_row(tree))
    session.flush()

    session.add(_idempotency_row(tree, route="POST /api/v1/files/copy"))
    session.flush()

    session.add(_idempotency_row(tree, principal_id=uuid.UUID(int=2)))
    session.flush()

    session.add(_idempotency_row(tree, request_hash="b" * 64))
    with pytest.raises(IntegrityError):
        session.flush()


def test_upload_session_round_trips_a_non_utf8_name(session: Session, tree: Tree) -> None:
    upload = tree.upload_session()
    session.expire_all()

    read = session.get(FileUploadSession, upload.id)
    assert read is not None
    assert read.name == UNDECODABLE_NAME
    with pytest.raises(UnicodeDecodeError):
        read.name.decode("utf-8")
    assert read.quota_hold_bytes == 4096
    assert read.quota_hold_nodes == 1
    assert read.transfer_mode == "proxied"
    assert read.lease_epoch == 7


def test_upload_part_composite_key_refuses_a_duplicate_part_number(
    session: Session, tree: Tree
) -> None:
    """A re-sent part lands once. The same part number in another session is a
    different part and must be accepted."""
    first = tree.upload_session()
    second = tree.upload_session()
    session.add(
        FileUploadPart(
            session_id=first.id,
            part_no=7,
            org_team_id=tree.org_team_id,
            size=1024,
            checksum=b"\x01\x02\xff",
            etag="etag-7",
        )
    )
    session.flush()

    session.add(
        FileUploadPart(
            session_id=second.id,
            part_no=7,
            org_team_id=tree.org_team_id,
            size=1024,
            checksum=b"\x01\x02\xff",
        )
    )
    session.flush()
    session.expire_all()

    read = session.get(FileUploadPart, (first.id, 7))
    assert read is not None
    assert read.checksum == b"\x01\x02\xff"

    # A Core insert, so the refusal comes from Postgres rather than from the
    # session's identity map noticing the duplicate first.
    with pytest.raises(IntegrityError):
        session.execute(
            insert(FileUploadPart).values(
                session_id=first.id,
                part_no=7,
                org_team_id=tree.org_team_id,
                size=2048,
                checksum=b"\x09",
            )
        )


def _lease(tree: Tree, node: FileNode, **overrides: object) -> FileLease:
    kwargs: dict[str, object] = {
        "node_id": node.id,
        "org_team_id": tree.org_team_id,
        "epoch": (1 << 32) + 5,
        "holder_principal_kind": "user",
        "holder_principal_id": uuid.uuid4(),
        "holder_instance_id": "instance-a",
        "machine_id": "machine-a",
        "purpose": "mount",
        "expires_at": EPOCH + timedelta(minutes=1),
        "last_sync_at": EPOCH,
    }
    kwargs.update(overrides)
    return FileLease(**kwargs)


def test_lease_round_trips_and_refuses_a_second_holder_on_one_node(
    session: Session, tree: Tree
) -> None:
    """The primary key IS the "one live lease per node" invariant: a second
    acquire on the same node cannot insert, whatever the service does."""
    folder = tree.node(b"mounted", parent_id=tree.root.id, ino=12, kind="folder")
    session.add(_lease(tree, folder))
    session.flush()
    session.expire_all()

    read = session.get(FileLease, folder.id)
    assert read is not None
    assert read.epoch == (1 << 32) + 5
    assert read.purpose == "mount"
    assert read.holder_instance_id == "instance-a"
    assert read.released_at is None

    # A Core insert, so the refusal comes from Postgres rather than from the
    # session's identity map noticing the duplicate first.
    with pytest.raises(IntegrityError):
        session.execute(
            insert(FileLease).values(
                node_id=folder.id,
                org_team_id=tree.org_team_id,
                epoch=1 << 33,
                holder_principal_kind="user",
                holder_principal_id=uuid.uuid4(),
                holder_instance_id="instance-b",
                machine_id="machine-b",
                purpose="box",
                expires_at=EPOCH + timedelta(minutes=1),
            )
        )


def test_lease_epoch_hwm_round_trips_an_epoch_above_the_lease(session: Session, tree: Tree) -> None:
    """The HWM survives a lease row that a restore lost, which is the whole
    point of keeping it in its own table."""
    folder = tree.node(b"held", parent_id=tree.root.id, ino=13, kind="folder")
    session.add(FileLeaseEpochHwm(node_id=folder.id, org_team_id=tree.org_team_id, hwm=1 << 40))
    session.flush()
    session.expire_all()

    read = session.get(FileLeaseEpochHwm, folder.id)
    assert read is not None
    assert read.hwm == 1 << 40


def test_stage_job_round_trips_its_resume_cursor(session: Session, tree: Tree) -> None:
    folder = tree.node(b"staged", parent_id=tree.root.id, ino=14, kind="folder")
    session.add(_lease(tree, folder))
    session.flush()
    job = FileStageJob(
        org_team_id=tree.org_team_id,
        lease_node_id=folder.id,
        machine_id="machine-a",
        direction="out",
        mode="bulk",
        state="running",
        cursor={"after": "n/abc", "batch": 3},
        bytes_total=1 << 30,
        bytes_done=1 << 20,
        failed_count=2,
        quarantined_count=1,
        lease_epoch=(1 << 32) + 5,
    )
    session.add(job)
    session.flush()
    session.expire_all()

    read = session.get(FileStageJob, job.id)
    assert read is not None
    assert read.cursor == {"after": "n/abc", "batch": 3}
    assert read.direction == "out"
    assert read.bytes_total == 1 << 30


def test_platform_row_is_the_seeded_singleton_and_admits_no_second(session: Session) -> None:
    """A second platform row is a constraint violation, not a convention: the
    restore generation has to be one number for the whole deployment.

    The migration seeds it, so the row is already there — the restore runbook
    increments it, it is never inserted a second time. The seeded value itself
    belongs to the migration test, which owns a fresh database; here another
    suite on the same database may already have bumped the generation, so what
    is asserted is the singleton and that a bump survives a re-read.
    """
    read = session.get(FilePlatform, 1)
    assert read is not None
    assert isinstance(read.restore_generation, int)
    assert read.restore_generation >= 0

    bumped = read.restore_generation + 1
    read.restore_generation = bumped
    session.flush()
    session.expire_all()
    reread = session.get(FilePlatform, 1)
    assert reread is not None and reread.restore_generation == bumped

    session.add(FilePlatform(id=2, restore_generation=4))
    with pytest.raises(IntegrityError):
        session.flush()


def test_sweep_shard_and_quarantine_round_trip(session: Session, tree: Tree) -> None:
    session.add(
        FileSweepShard(
            shard=4,
            holder="worker-1",
            expires_at=EPOCH + timedelta(minutes=5),
            cursor={"key": "objects/aa"},
            sweep_started_at=EPOCH,
        )
    )
    quarantine = FileQuarantine(
        org_team_id=tree.org_team_id,
        kind="object",
        ref_id="objects/aa/bb",
        reason="hash mismatch",
        attempts=3,
        detail={"expected": "aa", "actual": "bb"},
    )
    session.add(quarantine)
    session.flush()
    session.expire_all()

    shard = session.get(FileSweepShard, 4)
    assert shard is not None
    assert shard.cursor == {"key": "objects/aa"}
    read = session.get(FileQuarantine, quarantine.id)
    assert read is not None
    assert read.detail == {"expected": "aa", "actual": "bb"}
    assert read.attempts == 3


def test_later_tables_round_trip_their_rows(session: Session, tree: Tree) -> None:
    """The later tables exist now so a later phase adds rows, not a migration
    against a hot table — which is only true if they actually accept a row."""
    node = tree.node(b"shared.txt", parent_id=tree.root.id, ino=15)
    link = FileLink(
        org_team_id=tree.org_team_id,
        node_id=node.id,
        token_hash="t" * 64,
        scope="subtree",
        role="read",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$abc",
        expires_at=EPOCH + timedelta(days=1),
        params={"allowUpload": False},
        hide_download=True,
    )
    lock = FileLock(
        org_team_id=tree.org_team_id,
        node_id=node.id,
        holder_principal=uuid.uuid4(),
        kind="range",
        enforcement="mandatory",
        range_lo=0,
        range_hi=4095,
        token="lock-token",
        expires_at=EPOCH + timedelta(minutes=10),
    )
    hold = FileHold(
        org_team_id=tree.org_team_id,
        scope="subtree",
        node_id=node.id,
        matter_ref="matter-7",
        placed_by=uuid.uuid4(),
    )
    pack = FilePack(
        org_team_id=tree.org_team_id,
        dedup_domain_id=tree.domain.id,
        hash="p" * 64,
        store_id=tree.store.id,
        key="packs/p",
        stored_bytes=1000,
        raw_bytes=2000,
        chunk_count=10,
        live_bytes=1500,
        storage_class="STANDARD",
        sealed_at=EPOCH,
    )
    manifest = FileManifest(
        org_team_id=tree.org_team_id,
        dedup_domain_id=tree.domain.id,
        content_hash="c" * 64,
        size_bytes=2000,
        chunker="fastcdc",
        chunker_seed_version=1,
        term_count=1,
    )
    session.add_all([link, lock, hold, pack, manifest])
    session.flush()
    term = ManifestTerm(
        manifest_id=manifest.id,
        ord=0,
        org_team_id=tree.org_team_id,
        pack_id=pack.id,
        chunk_lo=0,
        chunk_hi=9,
        raw_bytes=2000,
    )
    chunk = FileKeyChunk(
        hmac_hash=b"\x00\xff\x10" * 5,
        org_team_id=tree.org_team_id,
        dedup_domain_id=tree.domain.id,
        pack_id=pack.id,
        chunk_idx=3,
    )
    snapshot = StorageUsageSnapshot(
        org_team_id=tree.org_team_id,
        drive_id=tree.drive.id,
        day=date(2026, 1, 1),
        billable_bytes=10,
        physical_bytes=20,
        head_bytes=5,
        version_bytes=3,
        trash_bytes=1,
        inline_bytes=1,
        egress_authorized_bytes=7,
        egress_reconciled_bytes=6,
        request_count=9,
    )
    erasure = FileErasureLog(
        org_team_id=tree.org_team_id,
        target=f"node:{node.id}",
        completed_at=EPOCH,
        certificate={"signed_by": "alkera", "objects": 2},
    )
    session.add_all([term, chunk, snapshot, erasure])
    session.flush()
    session.expire_all()

    assert session.get(FileLink, link.id) is not None
    read_lock = session.get(FileLock, lock.id)
    assert read_lock is not None and read_lock.range_hi == 4095
    assert session.get(FileHold, hold.id) is not None
    read_chunk = session.get(FileKeyChunk, b"\x00\xff\x10" * 5)
    assert read_chunk is not None and read_chunk.chunk_idx == 3
    read_term = session.get(ManifestTerm, (manifest.id, 0))
    assert read_term is not None and read_term.raw_bytes == 2000
    read_snapshot = session.get(
        StorageUsageSnapshot, (tree.org_team_id, tree.drive.id, date(2026, 1, 1))
    )
    assert read_snapshot is not None and read_snapshot.egress_reconciled_bytes == 6
    read_erasure = session.get(FileErasureLog, erasure.id)
    assert read_erasure is not None
    assert read_erasure.certificate == {"signed_by": "alkera", "objects": 2}


def test_manifest_is_unique_per_domain_and_content_hash(session: Session, tree: Tree) -> None:
    """Dedup's identity: one manifest per (domain, content hash), which is
    exactly the blast radius dedup is allowed to cross."""
    for _ in range(2):
        session.add(
            FileManifest(
                org_team_id=tree.org_team_id,
                dedup_domain_id=tree.domain.id,
                content_hash="d" * 64,
                size_bytes=1,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_file_key_chunks_is_created_unpartitioned(engine: Engine) -> None:
    """The chunk index is created unpartitioned, so a later change can
    partition it while it is still empty."""
    with engine.connect() as conn:
        partitioned = conn.execute(
            text("SELECT relkind FROM pg_class WHERE relname = 'file_key_chunks'")
        ).scalar_one()
    assert partitioned == "r"


@pytest.mark.parametrize(
    "table_name",
    ["file_idempotency_keys", "file_upload_sessions", "file_key_chunks"],
    ids=["idempotency", "upload_sessions", "key_chunks"],
)
def test_churn_tables_carry_the_autovacuum_setting(engine: Engine, table_name: str) -> None:
    """A table written per request and swept by a job cannot wait for the
    cluster's 20% default before it vacuums."""
    with engine.connect() as conn:
        reloptions = conn.execute(
            text("SELECT reloptions FROM pg_class WHERE relname = :name"),
            {"name": table_name},
        ).scalar_one()
    assert reloptions is not None
    assert "autovacuum_vacuum_scale_factor=0.01" in reloptions


def test_declared_churn_tables_match_the_ones_the_database_got(engine: Engine) -> None:
    """``FILES_CHURN_TABLES`` is what the migration will read, so it has to
    name every table that actually carries the setting."""
    declared = {table.name for table in FILES_CHURN_TABLES}
    with engine.connect() as conn:
        applied = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT relname FROM pg_class "
                    "WHERE relname LIKE 'file\\_%' "
                    "AND array_to_string(reloptions, ',') "
                    "LIKE '%autovacuum_vacuum_scale_factor=0.01%'"
                )
            )
        }
    assert declared == applied


@pytest.mark.parametrize("table", OWNED_TABLES, ids=[t.name for t in OWNED_TABLES])
def test_every_constraint_and_index_is_explicitly_named(table: Table) -> None:
    """An unnamed constraint gets a non-deterministic Postgres name that a
    later migration cannot drop by name."""
    for constraint in table.constraints:
        if isinstance(constraint, (UniqueConstraint, CheckConstraint, ForeignKeyConstraint)):
            assert constraint.name is not None, f"{table.name}: unnamed {type(constraint).__name__}"
            assert not str(constraint.name).startswith("_unnamed_"), table.name
    for index in table.indexes:
        assert index.name is not None, f"{table.name}: unnamed index"
        assert isinstance(index, Index)


@pytest.mark.parametrize("table", OWNED_TABLES, ids=[t.name for t in OWNED_TABLES])
def test_every_named_constraint_reaches_postgres_under_its_name(
    engine: Engine, table: Table
) -> None:
    """The names are only useful if the database actually has them."""
    declared = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, (UniqueConstraint, CheckConstraint, ForeignKeyConstraint))
        and constraint.name is not None
    }
    with engine.connect() as conn:
        live = {
            row[0]
            for row in conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:name AS regclass)"),
                {"name": table.name},
            )
        }
    assert declared <= live, f"{table.name}: missing {sorted(declared - live)}"


@pytest.mark.parametrize("table", OWNED_TABLES, ids=[t.name for t in OWNED_TABLES])
def test_every_tenant_table_carries_org_team_id(table: Table) -> None:
    """RLS is forced on every tenant table, so the column has to exist even
    where the natural owner is a parent row. ``file_platform`` and
    ``file_sweep_shards`` are the named exceptions."""
    platform_tables = {"file_platform", "file_sweep_shards"}
    if table.name in platform_tables:
        assert "org_team_id" not in table.c
        return
    assert "org_team_id" in table.c
    assert not table.c["org_team_id"].nullable


def test_models_package_exposes_every_files_model() -> None:
    """Alembic autogenerate walks ``Base.metadata``, which is only populated by
    what ``alkera_core.models`` imports — a model missing from here is a table
    the migration silently drops."""
    import alkera_core.models as models

    for model in MODELS:
        name = model.__name__
        assert name in models.__all__, f"{name} is not re-exported from alkera_core.models"
        assert getattr(models, name) is model
    metadata_tables = set(Base.metadata.tables)
    for model in MODELS:
        assert model.__tablename__ in metadata_tables


def test_models_package_registers_every_files_table_for_autogenerate() -> None:
    """A fresh interpreter that only imports ``alkera_core.models`` must see
    every Files table, which is exactly what Alembic does."""
    import alkera_core.models  # noqa: F401

    inspector = inspect(FileOp)
    assert inspector.local_table is not None
    expected = {table.name for table in OWNED_TABLES}
    assert expected <= set(Base.metadata.tables)
