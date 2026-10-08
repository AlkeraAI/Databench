"""The tables a notebook keeps beside its live document: its kernels, its runs
and who edited which cell.

None of them holds code or outputs. The code is the document's (and, at rest,
the file's); a run records the hash and size of each cell's submitted text,
never the text. Every row carries its org, and every table is a content table
under the ``tenant_isolation`` policy (``alkera_core.db.row_security``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.notebooks.schemas import RUN_STATUSES, RUN_TRIGGERS


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


#: A kernel's states, as the engine reports them.
KERNEL_STATES: Final[tuple[str, ...]] = (
    "absent",
    "starting",
    "idle",
    "busy",
    "restarting",
    "stopped",
)
#: Who acts on a notebook.
ACTOR_KINDS: Final[tuple[str, ...]] = ("person", "agent", "system")
#: The cell id pattern, as Postgres spells it.
CELL_ID_SQL_PATTERN: Final = "^[0-9a-hjkmnp-tv-z]{10}$"

KERNEL_STATE_CHECK: Final = _in("state", KERNEL_STATES)
RUN_STATUS_CHECK: Final = _in("status", RUN_STATUSES)
RUN_TRIGGER_CHECK: Final = _in("trigger", RUN_TRIGGERS)
ACTOR_KIND_CHECK: Final = _in("actor_kind", ACTOR_KINDS)
CELL_ID_CHECK: Final = f"cell_id ~ '{CELL_ID_SQL_PATTERN}'"


class NotebookKernel(Base):
    """A kernel serving one notebook on one machine. The id is the engine's;
    an event naming it is accepted only from this row's org and machine."""

    __tablename__ = "notebook_kernels"
    __table_args__ = (
        CheckConstraint(KERNEL_STATE_CHECK, name="ck_notebook_kernels_state"),
        CheckConstraint("seq >= 0", name="ck_notebook_kernels_seq_nonnegative"),
        Index("ix_notebook_kernels_org_item", "org_id", "item_id"),
    )

    kernel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_notebook_kernels_org_id_teams"),
        nullable=False,
    )
    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    machine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    env_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotebookRun(Base):
    """One run: who asked (a person, or an agent for its person), what, at
    which document frontier, the hash and size of each cell's submitted text,
    and how it went, followed from the engine's events. A run the box began
    (an agent's, a widget's) is recorded from its ``run.queued`` event."""

    __tablename__ = "notebook_runs"
    __table_args__ = (
        CheckConstraint(ACTOR_KIND_CHECK, name="ck_notebook_runs_actor_kind"),
        CheckConstraint(RUN_TRIGGER_CHECK, name="ck_notebook_runs_trigger"),
        CheckConstraint(RUN_STATUS_CHECK, name="ck_notebook_runs_status"),
        Index("ix_notebook_runs_org_item_created", "org_id", "item_id", "created_at"),
        Index(
            "uq_notebook_runs_client_run_id",
            "org_id",
            "item_id",
            "client_run_id",
            unique=True,
            postgresql_where=text("client_run_id IS NOT NULL"),
        ),
        Index(
            "uq_notebook_runs_engine_run_id",
            "org_id",
            "item_id",
            "engine_run_id",
            unique=True,
            postgresql_where=text("engine_run_id IS NOT NULL"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_notebook_runs_org_id_teams"),
        nullable=False,
    )
    drive_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kernel_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "notebook_kernels.kernel_id",
            ondelete="SET NULL",
            name="fk_notebook_runs_kernel_id_notebook_kernels",
        ),
        nullable=True,
    )
    client_run_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_notebook_runs_requested_by_users"),
        nullable=True,
    )
    requested_by_agent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_display: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    frontier: Mapped[str | None] = mapped_column(Text, nullable=True)
    frontier_included: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: ``cell id -> {"sha256": hex, "bytes": int}`` of the text submitted.
    submitted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Why the run ended as it did, in the engine's words.
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The id the engine names the run by on its events: ``run_id`` for a
    #: run the platform recorded first, the engine's own for one the box began.
    engine_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotebookEdit(Base):
    """Who edited a cell, and when last: one row per cell and actor, upserted
    on every edit (:func:`alkera_core.notebooks.edits.record_edits`). It
    answers "who else edited this cell in the last 15 s" and the activity
    digest."""

    __tablename__ = "notebook_edits"
    __table_args__ = (
        CheckConstraint(CELL_ID_CHECK, name="ck_notebook_edits_cell_id"),
        CheckConstraint(ACTOR_KIND_CHECK, name="ck_notebook_edits_actor_kind"),
        CheckConstraint("edits >= 1", name="ck_notebook_edits_edits_positive"),
        CheckConstraint("first_at <= last_at", name="ck_notebook_edits_ordered"),
        UniqueConstraint(
            "org_id", "item_id", "cell_id", "actor_key", name="uq_notebook_edits_cell_actor"
        ),
        Index("ix_notebook_edits_org_item_last", "org_id", "item_id", "last_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_notebook_edits_org_id_teams"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    cell_id: Mapped[str] = mapped_column(String(10), nullable=False)
    #: ``user:<uuid>``, ``agent:<id>`` or ``machine:<id>``.
    actor_key: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_notebook_edits_user_id_users"),
        nullable=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_display: Mapped[str] = mapped_column(String(255), nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edits: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    last_submit_id: Mapped[str | None] = mapped_column(String(48), nullable=True)


class NotebookPeer(Base):
    """The Loro peer one writer of a notebook writes its operations as: one
    per (agent session or box machine or person, document), kept for the
    document's epoch. A writer that keeps its peer keeps its edits its own in
    the document (an editor undoes them as one peer's, and draws them as one
    writer's); a new epoch, whose history restarts, mints a new one."""

    __tablename__ = "notebook_peers"
    __table_args__ = (
        CheckConstraint("epoch >= 1", name="ck_notebook_peers_epoch_positive"),
        CheckConstraint("loro_peer >= 1024", name="ck_notebook_peers_loro_peer_client_range"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_notebook_peers_org_id_teams"),
        primary_key=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    #: ``user:<uuid>``, ``agent:<id>`` or ``machine:<id>``.
    actor_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    loro_peer: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotebookEpochTail(Base):
    """A notebook document's last state in an epoch its history restarted
    from: the whole old document, its version there, and the version of the
    next epoch whose content is that state. An operation batch whose token
    names the old epoch is applied there and carried into the new one cell by
    cell (a history restart reseeds the same cell ids), so a writer that read
    the document before a restart loses nothing. Kept for the latest
    restarts only."""

    __tablename__ = "notebook_epoch_tails"
    __table_args__ = (
        CheckConstraint("epoch >= 1", name="ck_notebook_epoch_tails_epoch_positive"),
        CheckConstraint("next_epoch > epoch", name="ck_notebook_epoch_tails_next_after"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_notebook_epoch_tails_org_id_teams"),
        primary_key=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    next_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    next_base_vv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "ACTOR_KINDS",
    "ACTOR_KIND_CHECK",
    "CELL_ID_CHECK",
    "CELL_ID_SQL_PATTERN",
    "KERNEL_STATES",
    "KERNEL_STATE_CHECK",
    "RUN_STATUS_CHECK",
    "RUN_TRIGGER_CHECK",
    "NotebookEdit",
    "NotebookEpochTail",
    "NotebookKernel",
    "NotebookPeer",
    "NotebookRun",
]
