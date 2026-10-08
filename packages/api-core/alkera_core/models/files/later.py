"""The tables a later phase fills.

Every table here is created empty in the phase-0 migration and stays empty
until its feature lands — public links, locks, legal holds, the chunk index and
its packs, the billing snapshots and the erasure log. They are declared now
because the ledger's rule is that a later phase adds *rows*, never a migration
against a hot table: adding ``file_key_chunks`` to a live deployment later would
mean a schema change while the drive is serving traffic.

``file_key_chunks`` is deliberately UNPARTITIONED here: the chunk index lands
in its own change, and partitioning an empty table then is free,
while shipping a partitioned parent now would fix a partition scheme before
anything has measured it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Connection,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.files.history import CHURN_AUTOVACUUM_SCALE_FACTOR

#: What a public link reaches: the node alone, or everything under it.
LINK_SCOPES: tuple[str, ...] = ("node", "subtree")
#: The lock flavours the façades need to round-trip.
LOCK_KINDS: tuple[str, ...] = ("flock", "range", "webdav", "wopi", "checkout")
#: Whether a lock is a hint to a client or refused server-side.
LOCK_ENFORCEMENTS: tuple[str, ...] = ("advisory", "mandatory")
#: How wide a legal hold reaches.
HOLD_SCOPES: tuple[str, ...] = ("org", "drive", "subtree", "node", "custodian")


class FileLink(Base):
    """A public link. (Later.)"""

    __tablename__ = "file_links"
    __table_args__ = (
        # Redemption is one indexed read on the hash of the presented token;
        # the token itself is never stored.
        UniqueConstraint("token_hash", name="uq_file_links_token_hash"),
        # Serves the item's "shared links" pane and the revoke sweep.
        Index("ix_file_links_node_id", "node_id"),
        Index("ix_file_links_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_links_node_id_file_nodes"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(
        Enum(
            *LINK_SCOPES,
            name="ck_file_links_scope",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="node",
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    #: argon2id over the link password, when one is set.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hide_download: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileLock(Base):
    """A byte-range or whole-file lock held by a façade. (Later.)"""

    __tablename__ = "file_locks"
    __table_args__ = (
        # Serves the write path's lock check — every lock on a node — and the
        # referential check PostgreSQL runs against this column once per row a
        # ``DELETE FROM file_nodes`` removes. It replaces a partial index over
        # the same column whose ``expires_at IS NOT NULL`` predicate selected
        # every row anyway (the column is NOT NULL) but which no unqualified
        # lookup could be proven to satisfy, so the referential check fell back
        # to reading the whole lock table once per deleted node.
        Index("ix_file_locks_node_id", "node_id"),
        # Serves the reaper over lapsed locks.
        Index("ix_file_locks_expires_at", "expires_at"),
        Index("ix_file_locks_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_locks_node_id_file_nodes"),
        nullable=False,
    )
    holder_principal: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(
            *LOCK_KINDS,
            name="ck_file_locks_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    enforcement: Mapped[str] = mapped_column(
        Enum(
            *LOCK_ENFORCEMENTS,
            name="ck_file_locks_enforcement",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="advisory",
    )
    #: NULL/NULL is a whole-file lock.
    range_lo: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    range_hi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FileHold(Base):
    """A legal hold. Nothing under it is ever purged. (Later.)"""

    __tablename__ = "file_holds"
    __table_args__ = (
        # Serves the purge and erasure paths: the org's live holds, which they
        # must consult before deleting anything.
        Index(
            "ix_file_holds_org_live",
            "org_team_id",
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_file_holds_node_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope: Mapped[str] = mapped_column(
        Enum(
            *HOLD_SCOPES,
            name="ck_file_holds_scope",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: NULL for an org-wide or custodian-wide hold.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_holds_node_id_file_nodes"),
        nullable=True,
    )
    matter_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    placed_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FilePack(Base):
    """A sealed pack of chunks in the store. (Later.)"""

    __tablename__ = "file_packs"
    __table_args__ = (
        # A pack is addressed by its content hash inside its domain; the
        # uniqueness is what makes a re-sealed pack land once.
        UniqueConstraint("dedup_domain_id", "hash", name="uq_file_packs_domain_hash"),
        # Serves compaction: the packs in a domain with the least live bytes.
        Index("ix_file_packs_domain_live_bytes", "dedup_domain_id", "live_bytes"),
        Index("ix_file_packs_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    dedup_domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("dedup_domains.id", name="fk_file_packs_dedup_domain_id_dedup_domains"),
        nullable=False,
    )
    hash: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_stores.id", name="fk_file_packs_store_id_file_stores"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(1024), nullable=False)
    format_major: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    format_minor: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    raw_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Bytes still reachable; compaction rewrites a pack once this falls far
    #: enough below ``raw_bytes``.
    live_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    storage_class: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FileManifest(Base):
    """The chunk list behind one content hash. (Later.)"""

    __tablename__ = "file_manifests"
    __table_args__ = (
        # Content dedup's identity: one manifest per (domain, content hash),
        # which is exactly the blast radius dedup is allowed to cross.
        UniqueConstraint(
            "dedup_domain_id", "content_hash", name="uq_file_manifests_domain_content_hash"
        ),
        Index("ix_file_manifests_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    dedup_domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("dedup_domains.id", name="fk_file_manifests_dedup_domain_id_dedup_domains"),
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chunker: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    #: Which generation of the domain's chunker seed produced the boundaries.
    chunker_seed_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    term_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ManifestTerm(Base):
    """One run of a manifest: a byte range inside one pack. (Later.)"""

    __tablename__ = "manifest_terms"
    __table_args__ = (
        # Serves the read path: a manifest's terms in order, which is the whole
        # access pattern for reassembling content.
        Index("ix_manifest_terms_org_team_id", "org_team_id"),
        # Serves the GC's reverse walk: which manifests keep a pack alive.
        Index("ix_manifest_terms_pack_id", "pack_id"),
    )

    manifest_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "file_manifests.id",
            ondelete="CASCADE",
            name="fk_manifest_terms_manifest_id_file_manifests",
        ),
        primary_key=True,
    )
    ord: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    pack_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_packs.id", name="fk_manifest_terms_pack_id_file_packs"),
        nullable=False,
    )
    chunk_lo: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_hi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FileKeyChunk(Base):
    """The HMAC-keyed chunk index. Created UNPARTITIONED: the chunk-index
    change partitions this table while it is still empty, so no
    partitioning scheme is fixed before anything has measured one. (Later.)"""

    __tablename__ = "file_key_chunks"
    __table_args__ = (
        # Serves the GC and compaction: every chunk a pack holds.
        Index("ix_file_key_chunks_pack_id", "pack_id"),
        # Serves the domain-scoped dedup probe, which is the hot read.
        Index("ix_file_key_chunks_domain_last_seen", "dedup_domain_id", "last_seen_at"),
    )

    #: HMAC-keyed, so a dump of the index is not a dump of the corpus.
    hmac_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    dedup_domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("dedup_domains.id", name="fk_file_key_chunks_dedup_domain_id_dedup_domains"),
        nullable=False,
    )
    pack_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_packs.id", name="fk_file_key_chunks_pack_id_file_packs"),
        nullable=False,
    )
    chunk_idx: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StorageUsageSnapshot(Base):
    """One org-drive-day of billable storage. (Later.)"""

    __tablename__ = "storage_usage_snapshots"
    __table_args__ = (
        # Serves the billing read: an org's days in range. The primary key is
        # the day itself, so a re-run of the snapshot job overwrites rather
        # than double-counts.
        Index("ix_storage_usage_snapshots_org_day", "org_team_id", "day"),
    )

    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_drives.id", name="fk_storage_usage_snapshots_drive_id_file_drives"),
        primary_key=True,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    billable_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    physical_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    head_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    trash_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    inline_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: What we signed for, versus what the store actually reported.
    egress_authorized_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    egress_reconciled_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FileErasureLog(Base):
    """Append-only proof that an erasure happened, replayed on restore so a
    restored backup cannot resurrect erased content. (Later.)"""

    __tablename__ = "file_erasure_log"
    __table_args__ = (
        # Serves the restore replay: an org's erasures in the order they
        # happened.
        Index("ix_file_erasure_log_org_requested_at", "org_team_id", "requested_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: What was erased, spelled as text because it can be a node, a version or
    #: a whole drive; no FK, because the referent is gone by design.
    target: Mapped[str] = mapped_column(String(1024), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    certificate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


#: One row per stored chunk, touched on every dedup hit, so it cannot wait for
#: the cluster's 20% default before it vacuums; see ``ops.OPS_CHURN_TABLES``.
LATER_CHURN_TABLES = (FileKeyChunk.__table__,)

__all__ = [
    "HOLD_SCOPES",
    "LATER_CHURN_TABLES",
    "LINK_SCOPES",
    "LOCK_ENFORCEMENTS",
    "LOCK_KINDS",
    "FileErasureLog",
    "FileHold",
    "FileKeyChunk",
    "FileLink",
    "FileLock",
    "FileManifest",
    "FilePack",
    "ManifestTerm",
    "StorageUsageSnapshot",
]


@event.listens_for(FileKeyChunk.__table__, "after_create")
def _set_later_churn_autovacuum(target: Table, connection: Connection, **kw: Any) -> None:
    connection.execute(
        text(
            f"ALTER TABLE {target.name} SET "
            f"(autovacuum_vacuum_scale_factor = {CHURN_AUTOVACUUM_SCALE_FACTOR})"
        )
    )
