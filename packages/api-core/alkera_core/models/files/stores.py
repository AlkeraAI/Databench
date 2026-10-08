"""Where bytes live: object stores, dedup domains and per-org drives.

``file_stores`` is a **platform** table — one row per configured backend (the
filesystem driver in dev, an S3-compatible bucket in production) — so it is one
of the three Files tables that carry no ``org_team_id`` and are exempt from RLS.
Everything below it is tenant data: a ``dedup_domain`` is the blast radius of
content dedup (per org, per region, per store), and a ``file_drive`` is the one
row an org's whole tree hangs from.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: The store drivers a deployment can configure. ``filesystem`` is the dev and
#: self-hosted default; ``s3`` covers every S3-compatible endpoint (AWS, B2,
#: SeaweedFS), which is why the core path never makes an AWS-only call.
STORE_DRIVERS: tuple[str, ...] = ("filesystem", "s3")
#: How bytes reach the store for one upload.
TRANSFER_MODES: tuple[str, ...] = ("single", "proxied", "direct")
#: Reserved for a future per-user or per-project drive; today every drive is an
#: org drive, and the column exists so that day adds rows, not a migration.
DRIVE_KINDS: tuple[str, ...] = ("org",)
#: Why a drive refuses writes. NULL means the drive is writable.
DRIVE_FROZEN_REASONS: tuple[str, ...] = ("over_quota", "teardown")


class FileStore(Base):
    """A configured object store. Platform-scoped: no ``org_team_id``, no RLS."""

    __tablename__ = "file_stores"
    __table_args__ = (
        # One row per (driver, endpoint, bucket): the config loader upserts by
        # this key rather than minting a second row for the same bucket.
        UniqueConstraint(
            "driver", "endpoint", "bucket", name="uq_file_stores_driver_endpoint_bucket"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    driver: Mapped[str] = mapped_column(
        Enum(
            *STORE_DRIVERS,
            name="ck_file_stores_driver",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=False,
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    region: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    #: The ``StoreCapabilities`` record, dumped: conditional_write, presigned,
    #: range_signing, versioning, object_lock, lifecycle, scoped_credentials,
    #: storage_classes, strong_read_after_write, kms, limits.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    transfer_modes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)


class DedupDomain(Base):
    """The blast radius of content dedup: one org, one region, one store."""

    __tablename__ = "dedup_domains"
    __table_args__ = (
        # A domain is addressed as (org, region, store) by the store-handle
        # factory, and there is exactly one per triple.
        UniqueConstraint(
            "org_team_id", "region", "store_id", name="uq_dedup_domains_org_region_store"
        ),
        Index("ix_dedup_domains_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_stores.id", name="fk_dedup_domains_store_id_file_stores"),
        nullable=False,
    )
    #: A customer-managed KMS key, when the contract asks for one.
    kms_key_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Per-org chunk boundaries for the later chunk index — nothing to do with
    #: encryption, and unused on day one.
    chunker_seed: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    #: Names the HMAC key the later chunk index is keyed under, so a dump of the
    #: index is not a dump of the corpus.
    hmac_key_id: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: When this domain's prefix was confirmed to carry this deployment's
    #: ``meta/owner.json`` marker. NULL means the statement is still owed: the
    #: store was unreachable, slow past its deadline, or the row predates the
    #: marker. Nothing in a request reads it -- the collector reads the marker
    #: itself -- so the column exists only so the repair pass can find the
    #: domains whose marker is outstanding without asking the store about every
    #: domain on every sweep.
    owner_marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When this deployment found the prefix already marked by ANOTHER one. The
    #: marker is never overwritten, so such a domain is not ours and never will
    #: be: without somewhere to say so, the repair pass would ask the store
    #: about it again on every tick, forever. Set, it is also the list an
    #: operator reads to find the prefixes this database names but does not own.
    owner_marker_conflict_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the repair pass last ASKED the store about this prefix and got no
    #: answer. A store that will not answer leaves the domain owed, so without
    #: this the pass would re-read the same oldest page every tick and never
    #: reach the domains behind it; ordering by it makes the resume position a
    #: property of the rows, with no token to persist and none to feed back.
    owner_marker_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FileDrive(Base):
    """One row per org: the root of its tree and its quota."""

    __tablename__ = "file_drives"
    __table_args__ = (
        # One drive per org today; the uniqueness is what makes "the org's
        # drive" a single indexed read at session open.
        UniqueConstraint("org_team_id", "kind", name="uq_file_drives_org_team_kind"),
        # Nothing queries a drive by its root — PostgreSQL does. The column is a
        # foreign key into ``file_nodes``, so every row a ``DELETE FROM
        # file_nodes`` removes costs one referential check against it, and with
        # no index that check reads every drive in the database once per
        # deleted node.
        Index("ix_file_drives_root_node_id", "root_node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(
            *DRIVE_KINDS,
            name="ck_file_drives_kind",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=False,
        server_default="org",
    )
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_stores.id", name="fk_file_drives_store_id_file_stores"),
        nullable=False,
    )
    dedup_domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("dedup_domains.id", name="fk_file_drives_dedup_domain_id_dedup_domains"),
        nullable=False,
    )
    #: The tree's root node. Nullable and ``use_alter`` because file_nodes.drive_id
    #: points back here — the two tables are a cycle, created then linked.
    root_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_drives_root_node_id_file_nodes", use_alter=True),
        nullable=True,
    )
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quota_nodes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Inodes are block-allocated from here and never reused.
    next_ino: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    frozen_reason: Mapped[str | None] = mapped_column(
        Enum(
            *DRIVE_FROZEN_REASONS,
            name="ck_file_drives_frozen_reason",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=True,
    )


__all__ = [
    "DRIVE_FROZEN_REASONS",
    "DRIVE_KINDS",
    "STORE_DRIVERS",
    "TRANSFER_MODES",
    "DedupDomain",
    "FileDrive",
    "FileStore",
]
