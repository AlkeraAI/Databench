"""Folder leases, their epoch high-water mark, and the stage jobs they drive.

``file_leases`` is keyed by ``node_id`` because "one live lease per node" is an
invariant, and the cheapest place to state it is the primary key: a second
acquire on the same node cannot insert a second row, whatever the service does
above it. Overlap with an ancestor or a descendant is a service check against
``path_ids``; the key is the first net, not the only one.

Epochs are ``(restore_generation << 32) + seq``. ``file_lease_epoch_hwm`` is the
second net *within* one generation: a restore that loses lease rows cannot
re-issue an epoch it already handed out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.models.files.acl import PRINCIPAL_KINDS

#: Why a folder is leased. A compute box mounting a folder takes the same
#: ``box`` lease a desktop mount does. ``chat`` is the same claim taken for the
#: life of one awake chat: the box that runs it holds the chat's folder, and
#: sleeping returns it. ``workspace`` is the claim on a workspace's whole
#: folder for as long as its sandbox is awake: one writer for the shared tree
#: every chat in it works in.
LEASE_PURPOSES: tuple[str, ...] = ("mount", "box", "share", "chat", "workspace")
#: What kind of thing a lease is held BY — the vocabulary the fence compares
#: alongside ``holder_principal_id``. Every value but ``machine`` names a
#: principal the server authenticated from the credential on the request;
#: ``machine`` is a box whose assertion was checked against its registration.
#: There is no ``agent``: the agent assertion is two headers any member may put
#: on their own session, so an agent either proved a machine or holds nothing.
LEASE_HOLDER_KINDS: tuple[str, ...] = ("user", "pat", "service", "machine")
#: Which way a stage job moves bytes between the drive and a machine.
STAGE_DIRECTIONS: tuple[str, ...] = ("in", "out")
#: The stage job state machine, resumable from ``cursor``.
STAGE_STATES: tuple[str, ...] = ("queued", "running", "done", "failed", "cancelled")
#: What a live entry says is happening to one node RIGHT NOW, while the drive
#: still holds the previous copy. The first four are the holder pushing out
#: (a file being written, one whose bytes are on their way, one too large to
#: send that stays on the machine, one the bandwidth window has postponed);
#: the last three are the drive pushing in, and name which kind of change a
#: holder has yet to apply locally. The wire spelling is the same seven words
#: (``alkera_core.schemas.files.lease``), and a test pins the two together.
LIVE_ENTRY_STATES: tuple[str, ...] = (
    "writing",
    "uploading",
    "on_box",
    "deferred",
    "inbound",
    "inbound_delete",
    "inbound_rename",
)


class FileLease(Base):
    """Exclusive write on a folder's subtree for as long as the holder beats."""

    __tablename__ = "file_leases"
    __table_args__ = (
        # Serves the reaper: the leases whose TTL has passed and that nobody
        # has released, without scanning the released history.
        Index(
            "ix_file_leases_live_expires_at",
            "expires_at",
            postgresql_where=text("released_at IS NULL"),
        ),
        # Serves "which folders does this org have leased" for the UI badge and
        # the RLS-scoped read.
        Index("ix_file_leases_org_team_id", "org_team_id"),
        # Serves the client's "my mounts" list, keyed by the instance that
        # every fenced write carries.
        Index("ix_file_leases_holder_instance", "holder_principal_id", "holder_instance_id"),
    )

    #: The primary key IS the invariant: one live lease per node.
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_leases_node_id_file_nodes"),
        primary_key=True,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: ``(restore_generation << 32) + seq``, strictly increasing per node.
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    holder_principal_kind: Mapped[str] = mapped_column(
        Enum(
            *PRINCIPAL_KINDS,
            name="ck_file_leases_holder_principal_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: Which id space ``holder_principal_id`` is drawn from. A ``users`` row
    #: and a ``compute_allocations`` row are both UUIDs, so the fence compares
    #: the pair: without the kind, a member who can read a box's public machine
    #: id off a chat row would match the lease that box holds.
    holder_kind: Mapped[str] = mapped_column(
        Enum(
            *LEASE_HOLDER_KINDS,
            name="ck_file_leases_holder_kind",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="user",
    )
    holder_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: Generated by the client per mount and sent on every fenced write, so the
    #: same principal on two machines is two holders.
    holder_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(
        Enum(
            *LEASE_PURPOSES,
            name="ck_file_leases_purpose",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Advances with each committed snapshot; a holder whose heartbeat runs on
    #: while this stands still is reported ``stale``.
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: A lapsed or force-released lease is not re-grantable immediately.
    grantable_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reaped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Whether a write from someone who is NOT the holder is admitted into the
    #: subtree and handed to the holder to apply, instead of being refused. A
    #: desktop mount says no: the machine owns the tree. An awake chat says yes
    #: for its working directory, so a person can drop a file in while the agent
    #: runs; the chat's own records are refused either way.
    accepts_inbound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    #: Bumped by every change to the live plane, so a client that missed a frame
    #: knows it is behind without diffing the entries.
    live_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    #: The cadence block handed back on acquire and on every heartbeat: how long
    #: a change is coalesced, how often a batch goes out, how big a batch and a
    #: file may be. Stored per lease rather than read from settings at use, so a
    #: holder that acquired under one policy keeps the numbers it was told.
    live_cadence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: How many files the holder still had queued when it handed the folder
    #: back: bytes that never landed and stay on the machine. Written by the
    #: release, cleared by the next grant.
    unsynced_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: When the reaper finished with the rows this lease left behind once the
    #: grace ran out, so a later pass does not walk the same subtree again.
    #: Cleared by the next grant.
    unsynced_swept_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class FileLeaseEpochHwm(Base):
    """The highest epoch ever issued for a node, so a restore inside one
    generation cannot re-issue one it already handed out."""

    __tablename__ = "file_lease_epoch_hwm"
    __table_args__ = (Index("ix_file_lease_epoch_hwm_org_team_id", "org_team_id"),)

    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_lease_epoch_hwm_node_id_file_nodes"),
        primary_key=True,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    hwm: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class FileLeaseLiveEntry(Base):
    """One node the holder has reported an in-flight state for.

    The drive's copy of a leased subtree is the last checkpoint; between two
    checkpoints the machine holding the lease is the only place the current
    bytes exist. A row here is that gap made visible: this node is being
    written, or its bytes are on their way, or they are too big to send and
    stay on the machine — or the change is going the other way and the holder
    has not applied it yet. The row is deleted once the bytes land, so the
    table only ever holds what is genuinely in flight.

    Keyed by ``(lease_node_id, node_id)`` because a node is in exactly one
    state under exactly one lease; a second report replaces the first rather
    than queueing behind it. Both foreign keys cascade: the plane is meaningless
    without the lease that made it, and a purged node takes its entry with it.
    """

    __tablename__ = "file_lease_live_entries"
    __table_args__ = (
        # Serves "what is in flight under this lease" — the listing's facets and
        # the holder's own inbound drain, both org-scoped.
        Index("ix_file_lease_live_entries_org_lease", "org_team_id", "lease_node_id"),
        # Serves the per-node lookup an item read does.
        Index("ix_file_lease_live_entries_node", "node_id"),
    )

    lease_node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "file_leases.node_id",
            name="fk_file_lease_live_entries_lease_node_id_file_leases",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "file_nodes.id",
            name="fk_file_lease_live_entries_node_id_file_nodes",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    state: Mapped[str] = mapped_column(
        Enum(
            *LIVE_ENTRY_STATES,
            name="ck_file_lease_live_entries_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: The epoch the report was made under. A report from a fenced holder is
    #: dropped rather than applied, so a machine that came back from the dead
    #: cannot resurrect its old view of the tree.
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: What the machine says the file is, while the drive still holds the
    #: previous version — so a reader can show the size it is becoming.
    box_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    box_mtime_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: The lease's ``live_seq`` at the moment this entry was written.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FileStageJob(Base):
    """A staging pass between a leased folder and a machine. Created empty
    today; the code that fills it lands with the mount and the compute box."""

    __tablename__ = "file_stage_jobs"
    __table_args__ = (
        # Serves the reaper: a lapsed lease aborts its stage jobs at the
        # superseded epoch, in the same transaction.
        Index("ix_file_stage_jobs_lease_node_id", "lease_node_id"),
        # Serves the per-org job list and the RLS-scoped read.
        Index("ix_file_stage_jobs_org_state", "org_team_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    lease_node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_leases.node_id", name="fk_file_stage_jobs_lease_node_id_file_leases"),
        nullable=False,
    )
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(
        Enum(
            *STAGE_DIRECTIONS,
            name="ck_file_stage_jobs_direction",
            native_enum=False,
            length=8,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: How the bytes move (``stream``, ``bulk``, …); free text until the
    #: staging phase fixes the vocabulary.
    mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    state: Mapped[str] = mapped_column(
        Enum(
            *STAGE_STATES,
            name="ck_file_stage_jobs_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="queued",
    )
    #: Where a killed job resumes from.
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    bytes_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_done: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quarantined_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


__all__ = [
    "LEASE_HOLDER_KINDS",
    "LEASE_PURPOSES",
    "LIVE_ENTRY_STATES",
    "STAGE_DIRECTIONS",
    "STAGE_STATES",
    "FileLease",
    "FileLeaseEpochHwm",
    "FileLeaseLiveEntry",
    "FileStageJob",
]
