"""What happened to a node, and the small tables that hang off the tree.

``file_history`` is append-only and written in the **same transaction** as the
change it describes, so a mutation that lands without its history row is not a
state the database can be in.

``file_dir_stats`` is a cache fed by append-only ``file_dir_stats_deltas`` rows
that a job aggregates. Nothing in the write path ever increments an ancestor:
that is what makes a hot row structurally impossible, and it is why a folder's
size is a read of one cached row rather than a live SUM over a subtree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Connection,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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

#: The mutation kinds the history records. Every one of them is a row.
HISTORY_KINDS: tuple[str, ...] = (
    "create",
    "copy",
    "rename",
    "move",
    "attrs",
    "trash",
    "restore",
    "lock",
    "hold",
    "label",
    "acl",
    "conflict_resolved",
    "conflict",
)
#: A conflict's lifecycle. ``resolved`` and ``discarded`` both set ``resolved_at``.
#: ``auto`` is one the drive settled itself -- the last write to arrive kept the
#: name, the displaced bytes were kept beside it -- and that still waits for a
#: person to confirm, so it lists beside ``open`` until someone does.
CONFLICT_STATES: tuple[str, ...] = ("open", "resolved", "discarded", "auto")
#: The states the conflicts pane lists: the ones a person still has to act on.
CONFLICT_ACTIONABLE_STATES: tuple[str, ...] = ("open", "auto")
#: Which side's write reached the drive last and kept the name.
CONFLICT_ARRIVALS: tuple[str, ...] = ("web", "holder")


class FileHistory(Base):
    """Append-only, one row per mutation, in the mutation's own transaction."""

    __tablename__ = "file_history"
    __table_args__ = (
        # Dense per node: the item's history pane pages on it, and the
        # uniqueness is what makes a replayed write land once.
        UniqueConstraint("node_id", "seq", name="uq_file_history_node_seq"),
        # Serves the org-wide activity feed and the audit export.
        Index("ix_file_history_org_at", "org_team_id", "at"),
        # Serves "what did THIS principal touch lately", which is what the
        # Recent feed is a projection of. The index above cannot: its second
        # key is the timestamp, so a lookup by actor descends nothing and the
        # whole org's history is scanned for one person's last thirty days —
        # a cost that grows with the table rather than with the answer.
        Index("ix_file_history_org_principal_at", "org_team_id", "acting_principal", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_history_node_id_file_nodes"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(
            *HISTORY_KINDS,
            name="ck_file_history_kind",
            native_enum=False,
            # 32, not the 16 this started at: `conflict_resolved` is 17 bytes,
            # and a kind that does not fit its own column is why two callers
            # wrote their mutation under a neighbouring kind instead.
            length=32,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: The delegation chain, so "the agent did it on behalf of the user" is on
    #: the row rather than inferred later.
    acting_principal: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    delegating_user: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    agent_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: The operation this change belonged to, when it was part of a bulk one.
    op_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class FileRetentionLabel(Base):
    """A named retention policy a node can carry (day one: ``head-only``)."""

    __tablename__ = "file_retention_labels"
    __table_args__ = (
        # The label is addressed by name in the API and in policy, so it is
        # unique per org.
        UniqueConstraint("org_team_id", "name", name="uq_file_retention_labels_org_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: An admin-chosen policy label, not a filename — so text, unlike every
    #: node name in this schema, which is bytes.
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class FileStar(Base):
    """One user's star on one node."""

    __tablename__ = "file_stars"
    __table_args__ = (
        # Serves the "Starred" view: one user's stars, newest first.
        Index("ix_file_stars_org_user", "org_team_id", "user_id"),
        # The node is the LAST column of the primary key, so a lookup by node
        # alone cannot descend that index — PostgreSQL reads all of it. The
        # column is a foreign key into ``file_nodes`` with ``ON DELETE
        # CASCADE``, so that full index scan runs once per row a ``DELETE FROM
        # file_nodes`` removes.
        Index("ix_file_stars_node_id", "node_id"),
    )

    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", ondelete="CASCADE", name="fk_file_stars_node_id_file_nodes"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileConflict(Base):
    """A divergence a sync could not merge: base, theirs and mine, kept whole."""

    __tablename__ = "file_conflicts"
    __table_args__ = (
        # Serves the conflicts pane and the mount's "needs attention" badge:
        # the org's open conflicts.
        Index(
            "ix_file_conflicts_org_state",
            "org_team_id",
            "state",
            postgresql_where=text("state IN ('open', 'auto')"),
        ),
        Index("ix_file_conflicts_node_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_conflicts_node_id_file_nodes"),
        nullable=False,
    )
    #: NULL when the two sides were created independently and share no base.
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_versions.id", name="fk_file_conflicts_base_version_id_file_versions"),
        nullable=True,
    )
    theirs_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_versions.id", name="fk_file_conflicts_theirs_version_id_file_versions"),
        nullable=False,
    )
    mine_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_versions.id", name="fk_file_conflicts_mine_version_id_file_versions"),
        nullable=False,
    )
    actor: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    state: Mapped[str] = mapped_column(
        Enum(
            *CONFLICT_STATES,
            name="ck_file_conflicts_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="open",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The conflicted-copy node the displaced bytes were kept in, when the drive
    #: made one. NULL for a versions-only resolution (a machine-managed path, a
    #: copy past the drive's ceiling) and for every conflict a sync recorded.
    #: An id and not a foreign key: the copy is an ordinary file a person may
    #: purge, and a purge of it must not be blocked by the note that explains it.
    copy_node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: Which side reached the drive last and kept the name (``web``/``holder``).
    arrived_from: Mapped[str | None] = mapped_column(
        Enum(
            *CONFLICT_ARRIVALS,
            name="ck_file_conflicts_arrived_from",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=True,
    )
    #: Who wrote the bytes that kept the name — the last arrival — as a person
    #: reads it: a member's display name, a machine's name. Text resolved once,
    #: at the settlement, so the pane says what was true when it happened.
    who: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Who wrote the displaced bytes, resolved the same way. The copy is named
    #: after them, so this is the name its file already carries.
    displaced_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The actor who closed the conflict, as ``file_history.acting_principal``
    #: records one.
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileDirStatsDelta(Base):
    """An append-only increment for a folder, aggregated by a job — never in
    the write path, which is what keeps ancestors off the hot path."""

    __tablename__ = "file_dir_stats_deltas"
    __table_args__ = (
        # Serves the aggregator's sweep: every unaggregated delta for a folder.
        Index("ix_file_dir_stats_deltas_node_at", "node_id", "at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_dir_stats_deltas_node_id_file_nodes"),
        nullable=False,
    )
    bytes_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    files_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    direct_children_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: When a direct child was added or removed — the source of a folder's
    #: derived ``mtime``.
    child_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileDirStats(Base):
    """The aggregated cache a folder's size, count and ``mtime`` are read from."""

    __tablename__ = "file_dir_stats"
    __table_args__ = (Index("ix_file_dir_stats_org_team_id", "org_team_id"),)

    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "file_nodes.id", ondelete="CASCADE", name="fk_file_dir_stats_node_id_file_nodes"
        ),
        primary_key=True,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    files: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    direct_children: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_child_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FileTrashOp(Base):
    """One trash sweep, so a restore is one operation rather than a walk."""

    __tablename__ = "file_trash_ops"
    __table_args__ = (
        # Serves the purge janitor: everything whose grace period has expired.
        Index("ix_file_trash_ops_purge_after", "purge_after"),
        Index("ix_file_trash_ops_org_drive", "org_team_id", "drive_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_drives.id", name="fk_file_trash_ops_drive_id_file_drives"),
        nullable=False,
    )
    #: The subtree root that was trashed. No FK: file_nodes.trash_op_id already
    #: points here, and a second edge between the two would be a cycle for no
    #: gain.
    root_node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Why the drive trashed this, when nobody asked it to: ``left_on_machine``
    #: for a file whose bytes never left the machine that held its folder.
    #: ``None`` for every deletion a person or an agent made.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The machine the reason names, as its lease named it.
    reason_machine: Mapped[str | None] = mapped_column(String(128), nullable=True)


class FileContentGrant(Base):
    """A single signed content URL. A live grant is a GC root, which is why it
    is a row and not a claim inside a token."""

    __tablename__ = "file_content_grants"
    __table_args__ = (
        # Serves the expiry sweep and the GC's reachability pass, which counts
        # a live grant as a root.
        Index("ix_file_content_grants_expires_at", "expires_at"),
        Index("ix_file_content_grants_version_id", "version_id"),
    )

    #: The opaque nonce inside the signed URL; the primary key so redemption is
    #: one indexed read and a replay after ``used_at`` is visible.
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_versions.id", name="fk_file_content_grants_version_id_file_versions"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    range_lo: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    range_hi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


#: A table written per request and swept by a job cannot wait for the cluster's
#: 20% default before it vacuums. A Postgres storage parameter is not a
#: SQLAlchemy table argument, so the setting rides DDL attached to the table
#: itself — declared with the model, not only in the migration.
CHURN_AUTOVACUUM_SCALE_FACTOR = "0.01"
CHURN_TABLES = (FileContentGrant.__table__,)


@event.listens_for(FileContentGrant.__table__, "after_create")
def _set_churn_autovacuum(target: Table, connection: Connection, **kw: Any) -> None:
    connection.execute(
        text(
            f"ALTER TABLE {target.name} SET "
            f"(autovacuum_vacuum_scale_factor = {CHURN_AUTOVACUUM_SCALE_FACTOR})"
        )
    )


__all__ = [
    "CHURN_AUTOVACUUM_SCALE_FACTOR",
    "CHURN_TABLES",
    "CONFLICT_STATES",
    "HISTORY_KINDS",
    "FileConflict",
    "FileContentGrant",
    "FileDirStats",
    "FileDirStatsDelta",
    "FileHistory",
    "FileRetentionLabel",
    "FileStar",
    "FileTrashOp",
]
