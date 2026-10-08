"""Upload sessions and their parts.

A session is written **before** the first byte reaches the store, so an object
always has an owner: if the API dies after ``store.put`` and before the row is
updated, the session still names the object and the reconciliation sweeper
keeps it until the session expires rather than orphaning it.

The quota hold lives on the session row (``quota_hold_bytes`` /
``quota_hold_nodes``) because the invariant the tests assert is
``Σ holds == Σ open sessions``: a hold that lived anywhere else could survive
the session that took it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Connection,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Table,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.files.history import CHURN_AUTOVACUUM_SCALE_FACTOR
from alkera_core.models.files.stores import TRANSFER_MODES

#: The session state machine; each transition is one
#: ``UPDATE … WHERE state = $expected``.
UPLOAD_SESSION_STATES: tuple[str, ...] = (
    "open",
    "uploading",
    "committing",
    "done",
    "aborted",
    "expired",
)
#: The states in which a session still holds quota and still owns its objects.
UPLOAD_SESSION_LIVE_STATES: tuple[str, ...] = ("open", "uploading", "committing")


class FileUploadSession(Base):
    """One upload in flight: its target, its transfer mode and its quota hold."""

    __tablename__ = "file_upload_sessions"
    __table_args__ = (
        # Serves sweep_expired: the idle sessions whose TTL has passed, without
        # scanning the sessions that already reached a terminal state.
        Index(
            "ix_file_upload_sessions_live_expires_at",
            "expires_at",
            postgresql_where=text("state IN ('open', 'uploading', 'committing')"),
        ),
        # Serves the quota reservation read (Σ open holds for a drive) and the
        # per-drive session list.
        Index("ix_file_upload_sessions_org_drive", "org_team_id", "drive_id"),
        # Serves the fsck check "no committing session older than 1 h".
        Index(
            "ix_file_upload_sessions_committing",
            "expires_at",
            postgresql_where=text("state = 'committing'"),
        ),
        # Nothing queries a session by the node it targets or the folder it
        # lands in — PostgreSQL does. Both columns are foreign keys into
        # ``file_nodes``, so every row a ``DELETE FROM file_nodes`` removes
        # costs two referential checks here, and with no index each one reads
        # every session in the database.
        Index("ix_file_upload_sessions_node_id", "node_id"),
        Index("ix_file_upload_sessions_parent_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_drives.id", name="fk_file_upload_sessions_drive_id_file_drives"),
        nullable=False,
    )
    #: The node the commit will land on; NULL until a create resolves one.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_upload_sessions_node_id_file_nodes"),
        nullable=True,
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_upload_sessions_parent_id_file_nodes"),
        nullable=False,
    )
    #: The requested filename, as bytes — the commit has to reproduce exactly
    #: what the client sent, whatever encoding it was in.
    name: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dedup_domain_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "dedup_domains.id", name="fk_file_upload_sessions_dedup_domain_id_dedup_domains"
        ),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        Enum(
            *UPLOAD_SESSION_STATES,
            name="ck_file_upload_sessions_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="open",
    )
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: The store's own multipart id, so an abort can cancel it there too.
    store_upload_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    store_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transfer_mode: Mapped[str] = mapped_column(
        Enum(
            *TRANSFER_MODES,
            name="ck_file_upload_sessions_transfer_mode",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="proxied",
    )
    #: The folder lease epoch the session was opened under; a commit at a
    #: superseded epoch is fenced.
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Who held that lease, as ``file_leases.holder_principal_id`` records a
    #: holder. The commit half of an upload runs later and in another process —
    #: the Temporal worker acts as the platform janitor, not as the person who
    #: opened the session — so the caller at commit time is the wrong principal
    #: to fence on. NULL whenever ``lease_epoch`` is 0 (the ordinary upload).
    lease_holder: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: Which id space ``lease_holder`` is drawn from — ``file_leases.holder_kind``
    #: as it stood when the session was opened. Recorded beside the id for the
    #: same reason the lease row carries it: the commit re-presents the pair to
    #: the fence, and a uuid with no kind beside it could be matched against a
    #: holder of the other kind.
    lease_holder_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The bytes this session carries were displaced on the holder's disk, and
    #: land as a conflicted copy the DRIVE names rather than under ``name``.
    #: Only a fenced holder may open such a session.
    conflict_copy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The node those displaced bytes were the content of, when the holder
    #: knows it: the copy is filed beside it and linked to it by a conflict
    #: row. An id and not a foreign key: the session is a request, and a node
    #: purged while it is in flight is a refusal at commit, not a purge blocker.
    conflict_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    quota_hold_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quota_hold_nodes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Bumped on every accepted part, so an active upload never expires under
    #: the client.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileUploadPart(Base):
    """One accepted part. The composite key is what makes a re-sent part land
    once instead of twice."""

    __tablename__ = "file_upload_parts"
    __table_args__ = (
        # Serves the tenant scan the RLS policy and the sweeper both read.
        Index("ix_file_upload_parts_org_team_id", "org_team_id"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "file_upload_sessions.id",
            ondelete="CASCADE",
            name="fk_file_upload_parts_session_id_file_upload_sessions",
        ),
        primary_key=True,
    )
    part_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The client's checksum for this part; a re-sent part with a different
    #: checksum is refused rather than overwritten.
    checksum: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


#: A row per upload, swept continuously, so it cannot wait for the cluster's
#: 20% default before it vacuums; see ``ops.OPS_CHURN_TABLES`` for why the
#: storage parameter rides DDL rather than a table argument.
UPLOAD_CHURN_TABLES = (FileUploadSession.__table__,)


@event.listens_for(FileUploadSession.__table__, "after_create")
def _set_upload_churn_autovacuum(target: Table, connection: Connection, **kw: Any) -> None:
    connection.execute(
        text(
            f"ALTER TABLE {target.name} SET "
            f"(autovacuum_vacuum_scale_factor = {CHURN_AUTOVACUUM_SCALE_FACTOR})"
        )
    )


__all__ = [
    "UPLOAD_CHURN_TABLES",
    "UPLOAD_SESSION_LIVE_STATES",
    "UPLOAD_SESSION_STATES",
    "FileUploadPart",
    "FileUploadSession",
]
