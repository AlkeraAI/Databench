"""Operations and the idempotency ledger.

Every mutation that can take longer than a request — a bulk move, a copy, a
purge, an upload commit — is a ``file_ops`` row with its **inverse** written in
the same transaction as the change, which is what makes undo a forward
operation rather than a replay of guesswork.

``file_idempotency_keys`` is the other half of "a replay performs no second
effect": the stored response body is returned byte-for-byte for a repeated
``Idempotency-Key`` on the same route, so a retried ``conflict=rename`` cannot
mint ``report (1).pdf`` a second time.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Connection,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.files.history import CHURN_AUTOVACUUM_SCALE_FACTOR

#: What an operation does. The kind picks the inverse the undo path replays.
OP_KINDS: tuple[str, ...] = (
    "copy",
    "move",
    "trash",
    "restore",
    "purge",
    "upload",
    "download",
    "acl_rewrite",
    "undo",
    "bulk",
)
#: The operation state machine. ``running`` requires a heartbeat; the
#: watchdog marks a heartbeat-less operation ``failed``.
OP_STATES: tuple[str, ...] = ("queued", "running", "done", "failed", "cancelled")
#: An idempotency record's lifecycle: a row is claimed before the effect and
#: completed after it, so a concurrent replay sees ``in_progress``.
IDEMPOTENCY_STATUSES: tuple[str, ...] = ("in_progress", "succeeded", "failed")


class FileOp(Base):
    """One long-running or undoable operation, with its inverse."""

    __tablename__ = "file_ops"
    __table_args__ = (
        # A replayed create must return the operation it already made rather
        # than a second one, so the key is unique per org where it is present.
        Index(
            "uq_file_ops_org_idempotency_key",
            "org_team_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Serves GET /files/operations for a drive, newest first.
        Index("ix_file_ops_org_drive_created", "org_team_id", "drive_id", "created_at"),
        # Serves the watchdog: every running operation and its last heartbeat.
        Index(
            "ix_file_ops_running_heartbeat",
            "heartbeat_at",
            postgresql_where=text("state = 'running'"),
        ),
        # Serves the recovery of abandoned operations: the rows a lost
        # hand-off left `queued` with no runner, oldest first. Partial on the
        # same predicate the sweep reads, so the index holds only the rows in
        # flight rather than every operation the org has ever recorded -- a
        # tick on an idle fleet is then one scan of an index that is usually
        # empty, not a walk of the whole operation log every thirty seconds.
        Index(
            "ix_file_ops_queued_unclaimed",
            "created_at",
            postgresql_where=text("state = 'queued' AND heartbeat_at IS NULL"),
        ),
        # Serves the janitor's 90 d retention and the undo window check.
        Index(
            "ix_file_ops_undoable_until",
            "undoable_until",
            postgresql_where=text("undoable_until IS NOT NULL"),
        ),
        # Nothing queries an operation by its result — PostgreSQL does. The
        # column is a foreign key into ``file_nodes``, so every row a ``DELETE
        # FROM file_nodes`` removes costs one referential check against it, and
        # with no index that check reads the whole operation log once per
        # deleted node.
        Index("ix_file_ops_result_node_id", "result_node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    drive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_drives.id", name="fk_file_ops_drive_id_file_drives"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        Enum(
            *OP_KINDS,
            name="ck_file_ops_kind",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=False,
    )
    actor: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: What to do to put the tree back. Written in the mutation's transaction,
    #: never derived afterwards.
    inverse: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    undoable_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(
        Enum(
            *OP_STATES,
            name="ck_file_ops_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="queued",
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    conflicts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    result_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_ops_result_node_id_file_nodes"),
        nullable=True,
    )
    #: ``resultUrl`` and ``resultUrlExpiresAt`` for operation-shaped downloads.
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileIdempotencyKey(Base):
    """One retried request's claim and the answer it was given.

    Files keeps its replay records here for a day; other domains claim keys in
    the same table under their own ``scope`` (``alkera_core.idempotency``), and
    each row carries the ``expires_at`` its scope chose.
    """

    __tablename__ = "file_idempotency_keys"
    __table_args__ = (
        # The key is scoped to the principal AND the route: the same key on a
        # different route is a different request, and one principal's key can
        # never be replayed by another. That IS the primary key, named here so a
        # later migration can drop it by name; a second UNIQUE over the same
        # columns is one Postgres folds into the primary key on CREATE TABLE,
        # which would leave the model permanently drifted from the database.
        PrimaryKeyConstraint(
            "org_team_id",
            "key",
            "principal_id",
            "route",
            name="pk_file_idempotency_keys",
        ),
        # Serves the janitor, which drops an org's records past their expiry.
        Index("ix_file_idempotency_keys_org_expires_at", "org_team_id", "expires_at"),
    )

    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    route: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: The request body's hash: the same key with a different body is a 422,
    #: never a silent replay of the first answer.
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            *IDEMPOTENCY_STATUSES,
            name="ck_file_idempotency_keys_status",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="in_progress",
    )
    #: The stored response, returned byte-for-byte on a replay.
    body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The domain that claimed the key (``alkera_core.idempotency`` scopes).
    scope: Mapped[str] = mapped_column(String(64), nullable=False, server_default="files")
    #: When the janitor drops the record. Every claim sets it from its scope's
    #: retention; the default is Files' day, for a writer that names none.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() + interval '24 hours'"),
    )


#: Written on every non-GET and swept hourly, so it cannot wait for the
#: cluster's 20% default before it vacuums. A Postgres storage parameter is not
#: a SQLAlchemy table argument, so the setting rides DDL attached to the table
#: itself — declared with the model, not only in the migration.
OPS_CHURN_TABLES = (FileIdempotencyKey.__table__,)


@event.listens_for(FileIdempotencyKey.__table__, "after_create")
def _set_ops_churn_autovacuum(target: Table, connection: Connection, **kw: Any) -> None:
    connection.execute(
        text(
            f"ALTER TABLE {target.name} SET "
            f"(autovacuum_vacuum_scale_factor = {CHURN_AUTOVACUUM_SCALE_FACTOR})"
        )
    )


__all__ = [
    "IDEMPOTENCY_STATUSES",
    "OPS_CHURN_TABLES",
    "OP_KINDS",
    "OP_STATES",
    "FileIdempotencyKey",
    "FileOp",
]
