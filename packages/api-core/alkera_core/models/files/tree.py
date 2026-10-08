"""``file_nodes`` — the adjacency list that IS the tree.

The parent pointer is truth; ``path_ids`` is a derived ltree of ancestor **id**
labels (filenames cannot be ltree labels) so a subtree read is one indexed
``<@`` instead of a recursive CTE. Names are bytes, because a filename that came
off a Linux box is bytes and round-tripping it byte-exact is the whole point;
``name_key`` is the folded, searchable rendering and is what every index a
human's typing hits is built on.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.files._types import LTREE

#: Day one uses folder | file | object | symlink | special; ``shortcut``,
#: ``document`` and ``remote`` are named now so a later phase adds rows.
NODE_KINDS: tuple[str, ...] = (
    "folder",
    "file",
    "symlink",
    "shortcut",
    "object",
    "special",
    "document",
    "remote",
)
#: ``moving`` fences a large subtree move (writes into it 409 meanwhile);
#: ``locked`` is what an immutable/append-only flag or a legal hold sets.
NODE_STATES: tuple[str, ...] = ("live", "locked", "moving", "acl_rewriting")
#: Where the bytes came from, which is what decides whether attributes are
#: materialized on a shared box.
NODE_TRUST: tuple[str, ...] = ("own", "shared_in", "imported", "link_uploaded")
#: A symlink target is never followed server-side; the kind says how it is
#: stored and how it is rewritten on materialize and export.
SYMLINK_KINDS: tuple[str, ...] = ("relative", "canonical", "host")
#: Denormalized from the head version's sniffed type at commit, so the mime
#: filter chip is a SQL predicate and not a join.
MIME_CLASSES: tuple[str, ...] = (
    "image",
    "tabular",
    "code",
    "archive",
    "document",
    "text",
    "other",
)


class FileNode(Base):
    __tablename__ = "file_nodes"
    __table_args__ = (
        # The POSIX invariant: one live name per directory, byte-exact, so
        # ``README`` and ``readme`` are both live. Partial, so a trashed twin
        # keeps its name and restore can offer the conflict rename.
        Index(
            "uq_file_nodes_parent_name_live",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("trashed_at IS NULL"),
        ),
        # Serves GET /files/{parent}/children?orderBy=name and path resolution's
        # per-segment batched lookup.
        Index("ix_file_nodes_parent_name_key", "parent_id", "name_key"),
        # Serves GET /files/search?q= — trigram substring on the folded name,
        # scoped by drive_id in the query.
        Index(
            "ix_file_nodes_name_key_trgm",
            "name_key",
            postgresql_using="gin",
            postgresql_ops={"name_key": "gin_trgm_ops"},
        ),
        # Serves every subtree read (subtree listing, move, trash, quota).
        # The key is the path TRUNCATED to its first 128 labels, because a GiST
        # key holds the whole indexed value and an unbounded path eventually
        # cannot be placed on a page — an index over the full column starts
        # refusing inserts around depth sixty, while the spec accepts 1,024.
        # ``FilesRepo.subtree_predicate`` writes every subtree query against
        # this same expression, and adds the exact ``path_ids <@ …`` when the
        # root is deeper than the truncation. The ``gist_ltree_ops(siglen=32)``
        # opclass the index is built with lives in the migration alone: Postgres
        # does not report an opclass back through reflection, so spelling it
        # here would read as permanent drift.
        Index(
            "ix_file_nodes_path_ids",
            text("subpath(path_ids, 0, 128)"),
            postgresql_using="gist",
        ),
        # The listing budget (100k children, page of 500, p95 150 ms) is met
        # only if the page is an index-only scan: keyset on
        # (drive_id, parent_id, name_key) with every column the item shape
        # needs carried in the INCLUDE.
        Index(
            "ix_file_nodes_listing",
            "drive_id",
            "parent_id",
            "name_key",
            postgresql_include=[
                "id",
                "name",
                "kind",
                "size",
                "mtime_ns",
                "etag",
                "head_version_id",
                "acl_id",
            ],
        ),
        # An inode is unique per drive and never reused; the uniqueness is what
        # the mount façades address a node by.
        UniqueConstraint("drive_id", "ino", name="uq_file_nodes_drive_ino"),
        # Filter chip "owner": listing and search filtered by created_by.
        Index("ix_file_nodes_drive_parent_created_by", "drive_id", "parent_id", "created_by"),
        # A member's own storage: the live files they put in the drive, summed
        # by size, optionally under one team folder (the path rides along so
        # the subtree test needs no heap fetch). Partial, so it stays the size
        # of the live tree and never carries folders or trash.
        Index(
            "ix_file_nodes_drive_created_by_live_files",
            "drive_id",
            "created_by",
            postgresql_include=["size", "path_ids"],
            postgresql_where=text("trashed_at IS NULL AND kind = 'file'"),
        ),
        # Filter chip "type": listing and search filtered by mime class.
        Index("ix_file_nodes_drive_mime_class", "drive_id", "mime_class"),
        # Filter chip "trash": the trash view reads only trashed rows, so the
        # index is partial and stays small next to a live tree.
        Index(
            "ix_file_nodes_drive_trashed_at",
            "drive_id",
            "trashed_at",
            postgresql_where=text("trashed_at IS NOT NULL"),
        ),
        # The RLS predicate and every org-scoped sweep read this column.
        Index("ix_file_nodes_org_team_id", "org_team_id"),
        # Nothing queries by shortcut target — PostgreSQL does. The column is a
        # foreign key back into this same table, so every row a DELETE removes
        # costs one referential check against it, and with no index that check
        # is a sequential scan of every node in the database. Purging a subtree
        # of a thousand nodes therefore read the whole node table a thousand
        # times: the cost of a purge grew with the size of the tenant rather
        # than with the size of what was purged.
        Index("ix_file_nodes_target_id", "target_id"),
        # The invariant every path walker leans on: a name is one path
        # component. ``names.validate`` refuses a separator on every name a
        # caller proposes; this is the floor under the names the system derives
        # itself (a member's home, a team's folder), so one that slipped past
        # its derivation fails the insert rather than becoming a node that
        # ``root:``, a zip and a pull all split in two.
        CheckConstraint(
            "position('\\x2f'::bytea in name) = 0", name="ck_file_nodes_name_no_separator"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: Block-allocated from ``file_drives.next_ino``; never reused after purge.
    ino: Mapped[int] = mapped_column(BigInteger, nullable=False)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_drives.id", name="fk_file_nodes_drive_id_file_drives"),
        nullable=False,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: NULL only for the drive root.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_nodes_parent_id_file_nodes"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(
        Enum(
            *NODE_KINDS,
            name="ck_file_nodes_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: ``special`` only: fifo | socket | chardev | blockdev.
    subtype: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The filename as bytes, exactly as it arrived. Never text.
    name: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: The lossy, displayable rendering (U+FFFD for undecodable bytes).
    name_display: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    #: Folded and normalized: what search and ordering use.
    name_key: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    #: The encoding the bytes were decoded under, or ``binary``.
    name_encoding: Mapped[str] = mapped_column(String(32), nullable=False, server_default="utf-8")
    #: {windows_safe, macos_safe, display_warning} — computed at create so a
    #: mount on either platform warns without re-deriving it per listing.
    flags_names: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: The derived chain of ancestor id labels, root first, this node last.
    path_ids: Mapped[str] = mapped_column(LTREE, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ``shortcut`` target (later); never a shortcut itself.
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_nodes_target_id_file_nodes"),
        nullable=True,
    )
    #: ``object`` kind: the WorkspaceObject this node stands for. No FK — the
    #: objects table belongs to another subsystem and a Files migration must
    #: not take a lock on it.
    target_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: The current version. ``use_alter`` because file_versions.node_id points
    #: back here; the two tables are a cycle.
    head_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "file_versions.id",
            name="fk_file_nodes_head_version_id_file_versions",
            use_alter=True,
        ),
        nullable=True,
    )
    #: POSIX attributes: stored and round-tripped, NEVER evaluated for access.
    mode: Mapped[int] = mapped_column(Integer, nullable=False, default=0o644)
    uid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nlink: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rdev: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    atime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Server-derived; a client write of ``ctime`` is refused.
    ctime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    birthtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    xattrs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Stored and materialized verbatim; never followed server-side.
    symlink_target: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    symlink_kind: Mapped[str | None] = mapped_column(
        Enum(
            *SYMLINK_KINDS,
            name="ck_file_nodes_symlink_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=True,
    )
    mime_class: Mapped[str | None] = mapped_column(
        Enum(
            *MIME_CLASSES,
            name="ck_file_nodes_mime_class",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=True,
    )
    #: The change token a conditional write compares with ``If-Match``.
    etag: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    flags: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(
        Enum(
            *NODE_STATES,
            name="ck_file_nodes_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="live",
    )
    trust: Mapped[str] = mapped_column(
        Enum(
            *NODE_TRUST,
            name="ck_file_nodes_trust",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="own",
    )
    acl_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_acls.id", name="fk_file_nodes_acl_id_file_acls"),
        nullable=True,
    )
    #: Snapshot onto children at create, for folders.
    default_acl_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_acls.id", name="fk_file_nodes_default_acl_id_file_acls"),
        nullable=True,
    )
    #: The node is visible only as a path segment on the way to something the
    #: caller may read; its own contents are not listed.
    traversal_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retention_label_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "file_retention_labels.id",
            name="fk_file_nodes_retention_label_id_file_retention_labels",
        ),
        nullable=True,
    )
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The trash operation that swept this node, so restore is one op, not a walk.
    trash_op_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_trash_ops.id", name="fk_file_nodes_trash_op_id_file_trash_ops"),
        nullable=True,
    )
    #: What the machine holding this file's folder has on its disk, as it last
    #: reported it: size, modified time, the BLAKE3 of the bytes once it has
    #: computed one, and the lease ``live_seq`` of that report. All NULL on
    #: every node that is not a file under a live lease, and cleared again by a
    #: content commit whose version matches (``alkera_core.files.freshness``).
    holder_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_mtime_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    holder_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    holder_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Experimental fields, before one earns a typed column.
    node_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "MIME_CLASSES",
    "NODE_KINDS",
    "NODE_STATES",
    "NODE_TRUST",
    "SYMLINK_KINDS",
    "FileNode",
]
