"""The Files tree models against real Postgres.

Every table in the first Appendix A3 group gets a row written with every
non-null column set and read back, because the point of these models is the
fidelity of what they store: a filename that is not valid UTF-8 must come back
byte-identical, an ltree path must survive the custom type, and a JSONB bag
must not be flattened to text on the way through.

The index behaviour is asserted the same way — by making the database refuse a
write it must refuse (a second live sibling with the same name, a reused inode)
and accept one it must accept (the same name again once the first is trashed),
so removing the partial ``WHERE trashed_at IS NULL`` breaks a test in each
direction rather than only one.
"""

from __future__ import annotations

import os
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

# ``alkera_core.models`` for the whole registry, so the foreign keys into
# ``file_nodes.id`` can be discovered from the metadata instead of listed: a
# reference declared in a module this one never names is still one a purge has
# to pay for.
import alkera_core.models  # noqa: F401
import pytest
from alkera_core.db.base import Base
from alkera_core.models._enums import TeamRole
from alkera_core.models.files import (
    CHURN_AUTOVACUUM_SCALE_FACTOR,
    CHURN_TABLES,
    DedupDomain,
    FileAcl,
    FileAclMember,
    FileConflict,
    FileContentGrant,
    FileDirStats,
    FileDirStatsDelta,
    FileDrive,
    FileHistory,
    FileNode,
    FileRetentionLabel,
    FileShare,
    FileStar,
    FileStore,
    FileTrashOp,
    FileVersion,
)
from alkera_core.models.files._types import LTREE
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from sqlalchemy import Engine, Index, MetaData, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
)

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = pytest.mark.xdist_group("files_models_tree")

#: The tables this lane owns, in the order a reader wants to see them.
MODELS = (
    FileStore,
    DedupDomain,
    FileDrive,
    FileNode,
    FileAcl,
    FileAclMember,
    FileShare,
    FileHistory,
    FileRetentionLabel,
    FileStar,
    FileConflict,
    FileDirStatsDelta,
    FileDirStats,
    FileTrashOp,
    FileContentGrant,
    FileVersion,
)
TABLES = [model.__table__ for model in MODELS]

#: Every foreign key in the schema that points at ``file_nodes.id``, read off
#: the metadata rather than written down, so a table added tomorrow with an
#: unindexed reference to a node fails this module the day it lands.
NODE_REFERENCES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        {
            (table.name, fk.parent.name)
            for table in Base.metadata.sorted_tables
            for fk in table.foreign_keys
            if fk.column.table.name == "file_nodes" and fk.column.name == "id"
        }
    )
)

#: A filename Windows would reject and UTF-8 cannot decode: the exact thing the
#: bytea ``name`` column exists for.
UNDECODABLE_NAME = b"r\xe9sum\xe9\xff.txt"
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
    wanted = {table.name for table in TABLES} | {table for table, _ in NODE_REFERENCES}
    inspector = inspect(eng)
    missing = sorted(name for name in wanted if not inspector.has_table(name))
    if missing:
        eng.dispose()
        pytest.skip(f"the Files migrations are not applied to DATABASE_URL_SYNC: {missing}")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on its own transaction, rolled back so tests cannot see
    each other's rows."""
    connection = engine.connect()
    transaction = connection.begin()
    with Session(bind=connection) as db:
        yield db
    if transaction.is_active:
        transaction.rollback()
    connection.close()


class Tree:
    """The minimal chain every row in this schema hangs from."""

    def __init__(self, db: Session) -> None:
        self.db = db
        # A real org root team with a real member: the ``file_shares`` trigger
        # walks ``teams`` and ``team_memberships`` before it lets a share in, so
        # a share on an invented uuid is refused by the schema under test.
        self.org_team_id = uuid.uuid4()
        db.add(Team(id=self.org_team_id, name="Files org", is_root=True))
        db.flush()
        self.member_id = uuid.uuid4()
        db.add(
            User(
                id=self.member_id,
                home_org_team_id=self.org_team_id,
                email=f"member-{self.member_id.hex}@files.invalid",
                email_domain="files.invalid",
            )
        )
        db.flush()
        db.add(
            TeamMembership(user_id=self.member_id, team_id=self.org_team_id, role=TeamRole.MEMBER)
        )
        db.flush()
        self.store = FileStore(
            driver="s3",
            bucket="alkera-files",
            endpoint="https://s3.example.invalid",
            region="us-west-2",
            capabilities={"conditional_write": True, "presigned": True},
            transfer_modes=["single", "proxied", "direct"],
        )
        db.add(self.store)
        db.flush()
        self.domain = DedupDomain(
            org_team_id=self.org_team_id,
            region="us-west-2",
            store_id=self.store.id,
            kms_key_arn="arn:aws:kms:us-west-2:1:key/abc",
            chunker_seed=b"\x00\x01\xff",
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
            quota_nodes=1_000_000,
            next_ino=100,
        )
        db.add(self.drive)
        db.flush()
        self.acl = FileAcl(
            org_team_id=self.org_team_id,
            body=[{"principal_kind": "org", "principal_id": str(self.org_team_id), "role": "read"}],
            body_hash="a" * 64,
        )
        db.add(self.acl)
        db.flush()
        self.root = self.node(db, name=b"", parent_id=None, ino=1, kind="folder")
        self.drive.root_node_id = self.root.id
        db.flush()

    def node(
        self,
        db: Session,
        *,
        name: bytes,
        parent_id: uuid.UUID | None,
        ino: int,
        kind: str = "file",
        trashed_at: datetime | None = None,
        drive_id: uuid.UUID | None = None,
    ) -> FileNode:
        node = FileNode(
            ino=ino,
            drive_id=drive_id or self.drive.id,
            org_team_id=self.org_team_id,
            parent_id=parent_id,
            kind=kind,
            name=name,
            name_display=name.decode("utf-8", "replace"),
            name_key=name.decode("utf-8", "replace").casefold(),
            path_ids=f"n{uuid.uuid4().hex}",
            depth=0 if parent_id is None else 1,
            acl_id=self.acl.id,
            trashed_at=trashed_at,
        )
        db.add(node)
        db.flush()
        return node


@pytest.fixture
def tree(session: Session) -> Tree:
    return Tree(session)


def test_file_store_and_domain_and_drive_round_trip(session: Session, tree: Tree) -> None:
    session.expire_all()
    store = session.get(FileStore, tree.store.id)
    domain = session.get(DedupDomain, tree.domain.id)
    drive = session.get(FileDrive, tree.drive.id)
    assert store is not None and domain is not None and drive is not None
    assert store.driver == "s3"
    assert store.capabilities == {"conditional_write": True, "presigned": True}
    assert store.transfer_modes == ["single", "proxied", "direct"]
    # The chunker seed is raw key material, so it must come back as bytes, not
    # a hex string a driver decided to render.
    assert domain.chunker_seed == b"\x00\x01\xff"
    assert domain.kms_key_arn == "arn:aws:kms:us-west-2:1:key/abc"
    assert drive.quota_bytes == 1 << 40
    assert drive.next_ino == 100
    assert drive.root_node_id == tree.root.id
    assert drive.frozen_reason is None


def test_file_node_round_trips_bytes_name_ltree_and_jsonb(session: Session, tree: Tree) -> None:
    path = f"{tree.root.path_ids}.n{uuid.uuid4().hex}"
    node = FileNode(
        ino=101,
        drive_id=tree.drive.id,
        org_team_id=tree.org_team_id,
        parent_id=tree.root.id,
        kind="symlink",
        subtype=None,
        name=UNDECODABLE_NAME,
        name_display="r�sum��.txt",
        name_key="r�sum��.txt",
        name_encoding="binary",
        flags_names={"windows_safe": False, "macos_safe": True, "display_warning": True},
        path_ids=path,
        depth=1,
        mode=0o120777,
        uid=1000,
        gid=1000,
        nlink=1,
        size=17,
        rdev=0,
        atime_ns=1,
        mtime_ns=2,
        ctime_ns=3,
        birthtime_ns=4,
        xattrs={"user.tag": "blue"},
        symlink_target=b"../elsewhere\xff",
        symlink_kind="relative",
        mime_class="text",
        etag=7,
        flags=0,
        state="live",
        trust="imported",
        acl_id=tree.acl.id,
        traversal_only=False,
        node_metadata={"experiment": [1, 2, 3]},
        created_by=uuid.uuid4(),
    )
    session.add(node)
    session.flush()
    session.expire_all()

    read = session.get(FileNode, node.id)
    assert read is not None
    # The whole reason ``name`` is bytea: bytes no decoder can handle survive.
    assert read.name == UNDECODABLE_NAME
    assert read.symlink_target == b"../elsewhere\xff"
    assert read.path_ids == path
    assert read.xattrs == {"user.tag": "blue"}
    assert read.node_metadata == {"experiment": [1, 2, 3]}
    assert read.flags_names["windows_safe"] is False
    assert read.kind == "symlink"
    assert read.symlink_kind == "relative"
    assert read.trust == "imported"
    assert read.mode == 0o120777


def test_subtree_read_uses_the_ltree_containment_operator(session: Session, tree: Tree) -> None:
    child = tree.node(session, name=b"a", parent_id=tree.root.id, ino=110)
    child.path_ids = f"{tree.root.path_ids}.n{uuid.uuid4().hex}"
    session.flush()
    rows = (
        session.execute(
            text("SELECT id FROM file_nodes WHERE path_ids <@ CAST(:prefix AS ltree)"),
            {"prefix": tree.root.path_ids},
        )
        .scalars()
        .all()
    )
    assert set(rows) == {tree.root.id, child.id}


def test_a_second_live_sibling_with_the_same_name_is_refused(session: Session, tree: Tree) -> None:
    tree.node(session, name=b"report.json", parent_id=tree.root.id, ino=120)
    with pytest.raises(IntegrityError) as excinfo:
        tree.node(session, name=b"report.json", parent_id=tree.root.id, ino=121)
    assert "uq_file_nodes_parent_name_live" in str(excinfo.value)


def test_the_same_name_is_allowed_once_the_first_is_trashed(session: Session, tree: Tree) -> None:
    # The negative twin of the test above: the index is partial precisely so a
    # trashed row keeps its name and the live namespace is free again.
    tree.node(session, name=b"report.json", parent_id=tree.root.id, ino=130, trashed_at=EPOCH)
    live = tree.node(session, name=b"report.json", parent_id=tree.root.id, ino=131)
    assert live.id is not None


def test_names_differing_only_in_case_are_both_live(session: Session, tree: Tree) -> None:
    # Byte-exact uniqueness, not folded: a Linux tree with README and readme
    # imports without a rename.
    tree.node(session, name=b"README", parent_id=tree.root.id, ino=140)
    other = tree.node(session, name=b"readme", parent_id=tree.root.id, ino=141)
    assert other.id is not None


def test_an_inode_cannot_be_reused_within_a_drive(session: Session, tree: Tree) -> None:
    tree.node(session, name=b"one", parent_id=tree.root.id, ino=150)
    with pytest.raises(IntegrityError) as excinfo:
        tree.node(session, name=b"two", parent_id=tree.root.id, ino=150)
    assert "uq_file_nodes_drive_ino" in str(excinfo.value)


def test_acl_body_hash_is_the_interning_key(session: Session, tree: Tree) -> None:
    session.expire_all()
    acl = session.get(FileAcl, tree.acl.id)
    assert acl is not None
    assert acl.body[0]["role"] == "read"
    with pytest.raises(IntegrityError) as excinfo:
        session.add(FileAcl(org_team_id=tree.org_team_id, body=[], body_hash=acl.body_hash))
        session.flush()
    assert "uq_file_acls_body_hash" in str(excinfo.value)


def test_acl_member_and_share_round_trip(session: Session, tree: Tree) -> None:
    principal = uuid.uuid4()
    member = FileAclMember(
        acl_id=tree.acl.id,
        principal_kind="user",
        principal_id=principal,
        org_team_id=tree.org_team_id,
        max_role="write",
    )
    share = FileShare(
        org_team_id=tree.org_team_id,
        node_id=tree.root.id,
        principal_kind="user",
        principal_id=tree.member_id,
        role="read",
        expires_at=EPOCH + timedelta(days=7),
        conditions={"network": "corp"},
        granted_by=uuid.uuid4(),
    )
    session.add_all([member, share])
    session.flush()
    session.expire_all()

    read_member = session.get(FileAclMember, (tree.acl.id, "user", principal))
    read_share = session.get(FileShare, share.id)
    assert read_member is not None and read_member.max_role == "write"
    assert read_share is not None
    assert read_share.conditions == {"network": "corp"}
    assert read_share.revoked_at is None


@pytest.mark.parametrize(
    ("principal_kind", "of"),
    [
        pytest.param("user", "user", id="a_user_from_another_org"),
        pytest.param("team", "team", id="a_team_from_another_org"),
        pytest.param("org", "org", id="another_org_itself"),
    ],
)
def test_a_share_naming_a_principal_outside_the_org_is_refused(
    session: Session, tree: Tree, principal_kind: str, of: str
) -> None:
    """The first isolation layer is the database's, not the service's.

    A share row whose principal belongs to another org never reaches a policy —
    the ``file_shares`` trigger refuses the INSERT — so no service bug can put a
    foreign principal in an ACL chain.
    """
    other_org = uuid.uuid4()
    session.add(Team(id=other_org, name="Another org", is_root=True))
    session.flush()
    outsider = uuid.uuid4()
    if of == "user":
        session.add(
            User(
                id=outsider,
                home_org_team_id=other_org,
                email=f"outsider-{outsider.hex}@other.invalid",
                email_domain="other.invalid",
            )
        )
        session.add(TeamMembership(user_id=outsider, team_id=other_org, role=TeamRole.MEMBER))
        principal_id = outsider
    elif of == "team":
        session.add(Team(id=outsider, name="Another team", parent_team_id=other_org))
        principal_id = outsider
    else:
        principal_id = other_org
    session.flush()

    session.add(
        FileShare(
            org_team_id=tree.org_team_id,
            node_id=tree.root.id,
            principal_kind=principal_kind,
            principal_id=principal_id,
            role="read",
            granted_by=tree.member_id,
        )
    )
    with pytest.raises(IntegrityError) as excinfo:
        session.flush()
    assert "files.share_principal_not_in_org" in str(excinfo.value)


def test_history_is_dense_per_node_and_refuses_a_replayed_seq(session: Session, tree: Tree) -> None:
    row = FileHistory(
        org_team_id=tree.org_team_id,
        node_id=tree.root.id,
        seq=1,
        kind="rename",
        acting_principal=uuid.uuid4(),
        delegating_user=uuid.uuid4(),
        agent_session_id=uuid.uuid4(),
        before={"name": "a"},
        after={"name": "b"},
        op_id=uuid.uuid4(),
    )
    session.add(row)
    session.flush()
    session.expire_all()
    read = session.get(FileHistory, row.id)
    assert read is not None
    assert read.before == {"name": "a"} and read.after == {"name": "b"}
    assert read.at is not None

    with pytest.raises(IntegrityError) as excinfo:
        session.add(
            FileHistory(
                org_team_id=tree.org_team_id,
                node_id=tree.root.id,
                seq=1,
                kind="move",
                acting_principal=uuid.uuid4(),
            )
        )
        session.flush()
    assert "uq_file_history_node_seq" in str(excinfo.value)


def test_version_chain_round_trips_and_refuses_a_duplicate_seq(
    session: Session, tree: Tree
) -> None:
    node = tree.node(session, name=b"data.json", parent_id=tree.root.id, ino=200)
    version = FileVersion(
        org_team_id=tree.org_team_id,
        node_id=node.id,
        seq=1,
        size_bytes=12,
        content_hash="b" * 64,
        block_hash="c" * 64,
        inline_bytes=b'{"a": 1}\xff',
        store_key=None,
        mime_sniffed="application/json",
        scan_state="clean",
        scanned_at=EPOCH,
        scan_engine_version="clamav-1.3",
        source="upload",
        keep_forever=True,
        held=False,
        expires_at=EPOCH + timedelta(days=30),
        lease_epoch=4,
        version_metadata={"origin": "chat"},
        created_by=uuid.uuid4(),
    )
    session.add(version)
    session.flush()
    node.head_version_id = version.id
    session.flush()
    session.expire_all()

    read = session.get(FileVersion, version.id)
    assert read is not None
    assert read.inline_bytes == b'{"a": 1}\xff'
    assert read.manifest_id is None
    assert read.version_metadata == {"origin": "chat"}
    assert read.keep_forever is True
    read_node = session.get(FileNode, node.id)
    assert read_node is not None and read_node.head_version_id == version.id

    with pytest.raises(IntegrityError) as excinfo:
        session.add(
            FileVersion(
                org_team_id=tree.org_team_id,
                node_id=node.id,
                seq=1,
                content_hash="d" * 64,
                source="restore",
            )
        )
        session.flush()
    assert "uq_file_versions_node_seq" in str(excinfo.value)


def test_retention_label_star_conflict_and_stats_round_trip(session: Session, tree: Tree) -> None:
    node = tree.node(session, name=b"doc.txt", parent_id=tree.root.id, ino=210)
    label = FileRetentionLabel(
        org_team_id=tree.org_team_id, name="head-only", policy={"keep": "head"}
    )
    star = FileStar(org_team_id=tree.org_team_id, user_id=uuid.uuid4(), node_id=node.id)
    versions = [
        FileVersion(
            org_team_id=tree.org_team_id,
            node_id=node.id,
            seq=seq,
            content_hash=str(seq) * 8,
            source="upload",
        )
        for seq in (1, 2, 3)
    ]
    session.add_all([label, star, *versions])
    session.flush()
    conflict = FileConflict(
        org_team_id=tree.org_team_id,
        node_id=node.id,
        base_version_id=versions[0].id,
        theirs_version_id=versions[1].id,
        mine_version_id=versions[2].id,
        actor=uuid.uuid4(),
        state="open",
    )
    delta = FileDirStatsDelta(
        org_team_id=tree.org_team_id,
        node_id=tree.root.id,
        bytes_delta=1024,
        files_delta=1,
        direct_children_delta=1,
        child_change_at=EPOCH,
    )
    stats = FileDirStats(
        node_id=tree.root.id,
        org_team_id=tree.org_team_id,
        bytes=1024,
        files=1,
        direct_children=1,
        last_child_change_at=EPOCH,
    )
    session.add_all([conflict, delta, stats])
    session.flush()
    session.expire_all()

    assert session.get(FileRetentionLabel, label.id) is not None
    read_conflict = session.get(FileConflict, conflict.id)
    assert read_conflict is not None
    assert read_conflict.state == "open" and read_conflict.resolved_at is None
    read_stats = session.get(FileDirStats, tree.root.id)
    assert read_stats is not None and read_stats.bytes == 1024
    read_delta = session.get(FileDirStatsDelta, delta.id)
    assert read_delta is not None and read_delta.bytes_delta == 1024
    read_star = session.get(FileStar, (tree.org_team_id, star.user_id, node.id))
    assert read_star is not None


def test_trash_op_and_content_grant_round_trip(session: Session, tree: Tree) -> None:
    node = tree.node(session, name=b"gone.txt", parent_id=tree.root.id, ino=220)
    version = FileVersion(
        org_team_id=tree.org_team_id,
        node_id=node.id,
        seq=1,
        content_hash="e" * 64,
        source="upload",
    )
    trash_op = FileTrashOp(
        org_team_id=tree.org_team_id,
        drive_id=tree.drive.id,
        root_node_id=node.id,
        actor_id=uuid.uuid4(),
        purge_after=EPOCH + timedelta(days=30),
    )
    session.add_all([version, trash_op])
    session.flush()
    node.trashed_at = EPOCH
    node.trash_op_id = trash_op.id
    grant = FileContentGrant(
        nonce="n" * 43,
        org_team_id=tree.org_team_id,
        version_id=version.id,
        session_id=uuid.uuid4(),
        range_lo=0,
        range_hi=1023,
        expires_at=EPOCH + timedelta(minutes=5),
    )
    session.add(grant)
    session.flush()
    session.expire_all()

    read_grant = session.get(FileContentGrant, "n" * 43)
    assert read_grant is not None
    assert (read_grant.range_lo, read_grant.range_hi) == (0, 1023)
    # A grant is only a GC root while it is unredeemed and unexpired, so both
    # markers have to survive the round trip.
    assert read_grant.used_at is None
    read_node = session.get(FileNode, node.id)
    assert read_node is not None and read_node.trash_op_id == trash_op.id


@pytest.mark.parametrize("table", TABLES, ids=[t.name for t in TABLES])
def test_every_constraint_and_index_carries_an_explicit_name(table: Any) -> None:
    """An unnamed constraint gets a non-deterministic Postgres name that a later
    migration cannot drop, so the schema declares every one of them."""
    for index in table.indexes:
        assert isinstance(index.name, str), f"{table.name} has an unnamed index"
        assert index.name.startswith(("ix_", "uq_")), index.name
    for constraint in table.constraints:
        if not isinstance(constraint, (UniqueConstraint, CheckConstraint, ForeignKeyConstraint)):
            continue  # the primary key is named deterministically by Postgres
        assert isinstance(constraint.name, str), f"{table.name} has an unnamed constraint"
        assert constraint.name.startswith(("uq_", "ck_", "fk_")), constraint.name


def test_declared_names_are_the_names_postgres_actually_has(session: Session, tree: Tree) -> None:
    declared = {
        constraint.name
        for table in TABLES
        for constraint in table.constraints
        if isinstance(constraint, (UniqueConstraint, CheckConstraint, ForeignKeyConstraint))
        and isinstance(constraint.name, str)
    }
    present = set(
        session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conname = ANY(:names)",
            ),
            {"names": sorted(declared)},
        )
        .scalars()
        .all()
    )
    assert declared - present == set()


def test_the_listing_index_is_covering_and_the_hot_indexes_are_partial() -> None:
    """The listing performance budget is only met if the page is an index-only scan, so
    the covering columns are part of the schema's contract, not a hint."""
    by_name = {index.name: index for index in FileNode.__table__.indexes}
    listing = by_name["ix_file_nodes_listing"]
    assert listing.dialect_options["postgresql"]["include"] == [
        "id",
        "name",
        "kind",
        "size",
        "mtime_ns",
        "etag",
        "head_version_id",
        "acl_id",
    ]
    assert isinstance(by_name["uq_file_nodes_parent_name_live"], Index)
    assert (
        by_name["uq_file_nodes_parent_name_live"].dialect_options["postgresql"]["where"] is not None
    )
    assert (
        by_name["ix_file_nodes_drive_trashed_at"].dialect_options["postgresql"]["where"] is not None
    )
    assert by_name["ix_file_nodes_name_key_trgm"].dialect_options["postgresql"]["using"] == "gin"
    assert by_name["ix_file_nodes_path_ids"].dialect_options["postgresql"]["using"] == "gist"


def _referential_check(table: str, column: str) -> str:
    """The statement PostgreSQL's own referential trigger runs for every row a
    ``DELETE FROM file_nodes`` removes, spelled as ``ri_triggers.c`` builds it.

    It carries no predicate beyond the key, which is why a *partial* index over
    the referencing column cannot serve it however total that index's predicate
    happens to be.
    """
    return (
        f"SELECT 1 FROM ONLY {table} x "
        f"WHERE x.{column} OPERATOR(pg_catalog.=) :id FOR KEY SHARE OF x"
    )


#: The indexes on ``table`` that a lookup by ``column`` alone can descend: not
#: partial (the check above has no predicate to imply one with) and keyed on
#: ``column`` FIRST. A btree whose other columns lead is still usable as a
#: filter — PostgreSQL reports ``Index Scan`` and an ``Index Cond`` — but it
#: reads the whole index to do it, which is the very cost being removed here,
#: so the name of the index in the plan is what the assertion turns on.
DESCENDABLE_INDEXES = """
SELECT ci.relname
FROM pg_index i
JOIN pg_class ci ON ci.oid = i.indexrelid
JOIN pg_class ct ON ct.oid = i.indrelid
JOIN pg_attribute a ON a.attrelid = ct.oid AND a.attnum = i.indkey[0]
WHERE ct.relname = :table AND a.attname = :column AND i.indpred IS NULL AND i.indisvalid
"""


@pytest.mark.parametrize(
    ("table", "column"),
    NODE_REFERENCES,
    ids=[f"{table}.{column}" for table, column in NODE_REFERENCES],
)
def test_deleting_a_node_checks_every_reference_to_it_through_an_index(
    session: Session, tree: Tree, table: str, column: str
) -> None:
    """A purge must not read a whole table once per deleted node.

    Every foreign key into ``file_nodes.id`` makes PostgreSQL run the statement
    above once per removed row. Unindexed it is a sequential scan of the
    referencing table: purging a subtree of a thousand nodes read each of those
    tables a thousand times, and the cost of a purge was set by the size of the
    tenant rather than by the size of what was purged.

    The sequential fallback is turned off because a table this small is read
    end-to-end more cheaply than any index can be descended, whatever the
    predicate says — what is proven here is which index CAN serve the check, and
    a plan that still reads the table with the fallback off is one where nothing
    can.
    """
    session.execute(text("SET LOCAL enable_seqscan = off"))
    descendable = set(
        session.execute(text(DESCENDABLE_INDEXES), {"table": table, "column": column})
        .scalars()
        .all()
    )
    assert descendable, (
        f"{table}.{column} references file_nodes.id and no index leads with it, so the "
        "referential check PostgreSQL runs per deleted node has to read the whole table"
    )

    plan = "\n".join(
        session.execute(text(f"EXPLAIN {_referential_check(table, column)}"), {"id": tree.root.id})
        .scalars()
        .all()
    )

    assert any(name in plan for name in descendable), (
        f"expected one of {sorted(descendable)} in the plan for {table}.{column}\n{plan}"
    )
    assert "Seq Scan" not in plan, plan


def test_the_self_reference_index_is_the_one_a_purge_needs() -> None:
    """The index 0136 added is plain, and has to stay plain.

    Almost every node has a NULL ``target_id``, which reads as an invitation to
    make the index partial — but the referential check is issued for every
    deleted row, including those, and a partial index would send exactly the
    common case back to the sequential scan.
    """
    by_name = {index.name: index for index in FileNode.__table__.indexes}
    target = by_name["ix_file_nodes_target_id"]
    assert [column.name for column in target.columns] == ["target_id"]
    assert target.dialect_options["postgresql"]["where"] is None


@pytest.mark.parametrize("table_name", [t.name for t in CHURN_TABLES])
def test_churn_tables_carry_their_own_autovacuum_setting(session: Session, table_name: str) -> None:
    """A table written per request and swept by a job must not wait for the
    cluster's 20% default before it vacuums — and the setting has to be on the
    real table, not merely declared."""
    reloptions = session.execute(
        text("SELECT reloptions FROM pg_class WHERE relname = :name"),
        {"name": table_name},
    ).scalar_one()
    assert reloptions is not None
    assert f"autovacuum_vacuum_scale_factor={CHURN_AUTOVACUUM_SCALE_FACTOR}" in reloptions


def test_reflection_recognizes_the_ltree_column_without_warning(engine: Engine) -> None:
    """An unregistered type name is only a warning, and a silent one at that.

    SQLAlchemy resolves a reflected column by its Postgres type name; an
    unknown name warns and falls back to ``NullType``, which is precisely the
    shape autogenerate compares against — so a future change to ``path_ids``
    would produce no migration at all. Registering ``LTREE`` in the dialect's
    ``ischema_names`` is what makes reflection return the real type.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reflected = Table("file_nodes", MetaData(), autoload_with=engine)

    unrecognized = [str(w.message) for w in caught if "Did not recognize type" in str(w.message)]
    assert unrecognized == []
    assert isinstance(reflected.c.path_ids.type, LTREE)
