"""The event outbox: an append-only log of committed domain events.

One row per announced mutation, written in the SAME transaction as the mutation
(see ``alkera_core.events.outbox.emit``), so a row exists exactly when the change
it describes is durable. Two triggers installed by the migration (never modelled
here — Alembic does not compare triggers) complete the contract:

* ``trg_event_outbox_append_only`` refuses every UPDATE and DELETE, so the log
  is a record, not a table anyone can tidy;
* ``trg_event_outbox_notify`` sends ``pg_notify('alkera_events', id)`` after
  each insert a tenant stream receives, which reaches listeners when the
  transaction commits. A ``platform`` row rings nothing: Postgres serializes
  notifying commits database-wide, and the listener's poll reads it anyway.

``org_id`` deliberately has NO foreign key to ``teams``: a cascading delete would
be refused by the append-only trigger and make an org impossible to delete, and
a RESTRICT would do the same. Rows of a deleted org are unreadable by
construction (every reader filters by a live user's org). ``id`` is a
``bigserial`` — the only cursor consumers use; ``event_id`` is the row's stable
public identity. There is no retention in this version: the table only grows
(``TRUNCATE`` is intentionally not blocked, for an operator).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: The ``user:<uuid>`` visibility grammar. Repeated from
#: ``alkera_core.events.types.USER_VISIBILITY_PATTERN`` because this module
#: cannot import that package without a cycle; a test pins the two equal.
_USER_VISIBILITY_SQL_PATTERN = "^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: Repeated from ``alkera_core.events.outbox.MAX_PAYLOAD_BYTES``; a test pins the
#: two equal, and a migration moves this one.
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_outbox_event_id"),
        # Catch-up reads are `WHERE org_id = ? AND id > ? ORDER BY id`.
        Index("ix_event_outbox_org_id_id", "org_id", "id"),
        CheckConstraint(
            f"visibility IN ('org', 'platform') OR visibility ~ '{_USER_VISIBILITY_SQL_PATTERN}'",
            name="ck_event_outbox_visibility",
        ),
        CheckConstraint("version >= 0", name="ck_event_outbox_version_nonnegative"),
        CheckConstraint(
            f"octet_length(payload::text) <= {_MAX_PAYLOAD_BYTES}",
            name="ck_event_outbox_payload_size",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    visibility: Mapped[str] = mapped_column(
        String(64), nullable=False, default="org", server_default=text("'org'")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
