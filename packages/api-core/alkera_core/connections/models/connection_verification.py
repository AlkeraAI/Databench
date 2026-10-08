"""One verification attempt against a connection's configuration + credential.

A verification is the only thing that ever describes a check. It is requested
by a save (against a candidate payload that is not yet a row), by an explicit
verify, by a rotation, or by the hourly sweep; it moves through
``queued -> running -> settled`` and is abandoned when nobody picks it up or the
worker that had it went silent.

Dispatch facts live HERE, not in the process that asked. A replica polling a
record can tell "the server is still trying to reach the worker" from "the
worker has it" from "nobody has it" without having been the replica that sent
the first nudge — which is what makes the add dialog behave identically behind a
load balancer.

A record with no ``connection_id`` is a *draft*: the candidate configuration and
its independently encrypted credential roles, dialled before anything joins
``team_connections``. Its ciphertexts are cleared the moment the dial settles and
its ``payload_hash`` binds the verdict to the exact payload that was verified, so
the save that consumes it cannot quietly store something else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.connections import Outcome, VerificationState
from alkera_core.db.base import Base
from alkera_core.db.retired import retired_column, unmapped

_STATES = "(" + ",".join(f"'{s.value}'" for s in VerificationState) + ")"
_OUTCOMES = "(" + ",".join(f"'{o.value}'" for o in Outcome) + ")"
_VANTAGES = "('server')"

DETAIL_CEILING_CHARS = 16 * 1024
"""The only bound on a connector's sentence, and it is not a column width.

``detail`` is the whole of what an admin gets told about a refusal, and a real
one runs long: a driver that tried four hosts names each one, its address and
its own error, and the sentence that explains the failure is the last line of
it. Cutting that at a character count threw away the part the admin needed, so
the column is unbounded ``Text`` and this is the one guard left — a ceiling wide
enough that no driver reaches it, narrow enough that a pathological payload
cannot be stored through this path.
"""


def clamp_detail(detail: str) -> str:
    """The one place a verdict's sentence is bounded, for every writer of it.

    Under the ceiling — which every real driver message is — the text is
    returned exactly as it came, newlines and all. Over it, the cut is marked,
    so a reader can tell a clipped sentence from a short one.
    """
    if len(detail) <= DETAIL_CEILING_CHARS:
        return detail
    return detail[: DETAIL_CEILING_CHARS - 1] + "\u2026"


#: Who ran the check. Only the server ever dialed, so every row says ``server``
#: and nothing reads it; a later contract migration drops it with its check
#: (see deploy-notes).
_RETIRED = (retired_column("vantage_kind", String(16), nullable=False, server_default="server"),)


class ConnectionVerification(Base):
    __tablename__ = "connection_verifications"
    __table_args__ = (
        *_RETIRED,
        CheckConstraint(f"state IN {_STATES}", name="ck_cv_state"),
        CheckConstraint(f"outcome IS NULL OR outcome IN {_OUTCOMES}", name="ck_cv_outcome"),
        CheckConstraint(f"vantage_kind IN {_VANTAGES}", name="ck_cv_vantage"),
        Index("ix_cv_state_created", "state", "created_at"),
        Index("ix_cv_created", "created_at"),
        Index("ix_cv_connection_finished", "connection_id", "finished_at"),
    )
    __mapper_args__ = unmapped(_RETIRED)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL on a draft — the candidate payload has no row yet.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("team_connections.id", ondelete="CASCADE"),
        nullable=True,
    )
    plugin: Mapped[str] = mapped_column(String(64), nullable=False)
    # The BUILT (non-secret) attributes to dial — same shape as a connection's.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # The candidate secret, encrypted at rest; cleared when the dial settles.
    secret_encrypted: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    named_secrets_encrypted: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    #: What a settled verification found. NULL while it is still in flight.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Unbounded on purpose: a driver's multi-host failure list is the answer,
    #: and a width here is what used to cut it mid-sentence.
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    endpoint_results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Binds a draft's verdict to the exact payload it was produced for.
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- the hand-off to the worker, on the row rather than in a process ---
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_dispatch_error: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")

    # --- the run itself ---
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: When it was requested — the wire calls this ``requested_at``.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# What erasing an account does to the columns here that name a person. Spelled
# beside the table so the two always load together.
dispositions.register(
    Disposition(
        "connection_verifications", "requested_by_id", Kind.KEEP, dispositions.AUTHOR_REASON
    )
)
