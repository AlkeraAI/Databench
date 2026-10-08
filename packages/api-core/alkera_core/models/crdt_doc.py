"""The Loro CRDT lane's durable state: one document, its update log and the
Loro peers it has handed out.

``crdt_docs`` holds one row per (org, document type, document id) — the org is
in the key for the same reason as ``realtime_docs``: a chat id is chosen by a
client, so two tenants can name the same one. A row is the document at
``log_seq`` within ``epoch``: the last full Loro ``snapshot`` (taken at
``snapshot_log_seq``) plus every ``crdt_updates`` row of the epoch after it,
imported in ``log_seq`` order. ``vv`` is the encoded Loro version vector after
the last update and ``projection`` the plain-JSON view of the document a reader
without Loro uses (the draft's text, its digest, who wrote last). ``log_seq``
is a storage counter only — the wire carries version vectors, never it.

``epoch`` moves only when the history is replaced: rotation (the log and
snapshot outgrew their bound and the document restarts from its current
content), a quarantine re-seed (the stored state failed verification) or a
schema upgrade. ``quarantined_at`` marks one that must not be served until
re-seeded.

``crdt_peers`` binds every Loro peer id the server hands out to the person and
document it was minted for. Ids come from ``crdt_peer_seq``, which starts above
the range the server keeps for itself and never hands one out twice — a Loro
peer reused by two writers would mint colliding operation ids and split the
document — and stays below 2**53 so it survives a JavaScript number. A row
records which socket holds the peer right now (``held_by`` until
``held_until``), so a second tab offering the same id gets a fresh one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, NotRequired, TypedDict

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Sequence,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.doc_type_names import STORED_DOC_TYPES

#: A session holding edits its source does not have yet: the sweep's predicate,
#: and the partial index that serves it, spelled once.
CRDT_UNSAVED_PREDICATE = (
    "source_etag IS NOT NULL AND quarantined_at IS NULL AND projection ? 'sha256' "
    "AND (projection ->> 'sha256') <> source_sha256"
)

#: The document types a ``crdt_docs`` row may hold, in their stored spelling.
CRDT_DOC_TYPE_CHECK = "doc_type IN ('chat_workspace', 'file', 'notebook')"


def stored_doc_type(doc_type: str) -> str:
    """The ``doc_type`` column value for a document of wire type ``doc_type``."""
    return STORED_DOC_TYPES.get(doc_type, doc_type)


class SourceRecord(TypedDict):
    """One earlier source version a document matched: its etag, its version id
    (``None`` for a file with no version), the document's version vector
    there (standard base64), and when it stopped being the latest (Unix
    seconds; absent on a record written before that was kept)."""

    etag: int
    version_id: str | None
    vv: str
    at: NotRequired[float]


class PeerView(TypedDict):
    """One state of a document a text peer was handed (read, or the answer to
    a write), which its disk may hold: the epoch, the document's version
    vector there (standard base64), the source etag current then, and when
    (Unix seconds). The state holding exactly what a write sent names that
    write (``submit``), so a retry of it is recognised after the log that
    held it has been folded into a snapshot."""

    epoch: int
    vv: str
    etag: int
    at: float
    submit: NotRequired[str]


#: A document's source is recorded whole or not at all (a file with no
#: version yet has an etag but no version id).
CRDT_SOURCE_WHOLE_CHECK = (
    "(source_etag IS NULL AND source_version_id IS NULL AND source_sha256 IS NULL"
    " AND source_vv IS NULL AND source_epoch IS NULL)"
    " OR (source_etag IS NOT NULL AND source_sha256 IS NOT NULL"
    " AND source_vv IS NOT NULL AND source_epoch IS NOT NULL)"
)
#: The first Loro peer id handed to a client; everything below is the server's.
CRDT_FIRST_CLIENT_PEER = 1024
#: The last Loro peer id that survives a JavaScript number.
CRDT_LAST_PEER = 2**53 - 1

crdt_peer_seq = Sequence(
    "crdt_peer_seq",
    start=CRDT_FIRST_CLIENT_PEER,
    minvalue=CRDT_FIRST_CLIENT_PEER,
    maxvalue=CRDT_LAST_PEER,
    metadata=Base.metadata,
)


class CrdtDoc(Base):
    __tablename__ = "crdt_docs"
    __table_args__ = (
        CheckConstraint(CRDT_DOC_TYPE_CHECK, name="ck_crdt_docs_doc_type"),
        CheckConstraint("epoch >= 1", name="ck_crdt_docs_epoch_positive"),
        CheckConstraint("log_seq >= 0", name="ck_crdt_docs_log_seq_nonnegative"),
        CheckConstraint(
            "snapshot_log_seq >= 0 AND snapshot_log_seq <= log_seq",
            name="ck_crdt_docs_snapshot_within_log",
        ),
        CheckConstraint(
            "snapshot_bytes >= 0 AND log_bytes >= 0", name="ck_crdt_docs_bytes_nonnegative"
        ),
        CheckConstraint("doc_schema >= 1", name="ck_crdt_docs_doc_schema_positive"),
        CheckConstraint(CRDT_SOURCE_WHOLE_CHECK, name="ck_crdt_docs_source_whole"),
        Index("ix_crdt_docs_org_id", "org_id"),
        # The sweep's question, "which sessions hold edits not on the drive,
        # due a try, oldest first", answered from an index rather than a scan.
        Index(
            "ix_crdt_docs_unsaved",
            "save_retry_at",
            "updated_at",
            postgresql_where=text(CRDT_UNSAVED_PREDICATE),
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_crdt_docs_org_id_teams"),
        primary_key=True,
    )
    doc_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: Which life of this document the row is. A row deleted and created
    #: again restarts at epoch 1, log position 0: a worker still holding the
    #: old one at that position must not take it for the new one, so caches
    #: are keyed by this as well.
    incarnation: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    log_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    vv: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=text("'\\x'::bytea")
    )
    snapshot: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, default=b"", server_default=text("'\\x'::bytea")
    )
    snapshot_log_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    snapshot_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    log_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    doc_schema: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    #: The Loro version that wrote ``snapshot``.
    loro_format: Mapped[str] = mapped_column(String(32), nullable=False)
    projection: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Where this epoch's first content came from (``empty``, ``legacy_draft``,
    #: ``rotation``, ``quarantine``, or for a file the drive version it was
    #: read from, ``file_version:<id>:<content hash>``).
    seeded_from: Mapped[str] = mapped_column(String(160), nullable=False)
    #: For a document whose truth at rest is a source it is written back to
    #: (a file on the drive), the source version its content last matched: its
    #: seed, or its last write back. ``source_etag`` is the precondition the
    #: next write back names; ``source_sha256`` the digest of the content
    #: there; ``source_vv`` the document's version (in ``source_epoch``) whose
    #: content that is, from which a change made to the source outside the
    #: session is merged. All unset for a document with no source.
    source_etag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_vv: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The source versions it matched before that, newest first and in
    #: ``source_epoch`` only (see :class:`SourceRecord`): an outside change
    #: made on an older version of the source (an agent that read the file
    #: before the last write back reached its disk) is merged from that one.
    source_history: Mapped[list[SourceRecord]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the last write back of this session's edits was refused (``leased``,
    #: ``no_writer``, a drive's code), while it still is; when the sweep may
    #: try it again; how many refusals in a row. A session that cannot save
    #: is parked on a widening wait instead of being tried every few seconds
    #: by every replica, and the oldest sessions due a try go first. Cleared
    #: by the write back that lands.
    save_paused_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    save_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    save_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Until when the machine holding the file's folder counts as its text
    #: peer (it read or wrote the document as text lately): only then is it
    #: told on its machine channel that the document moved.
    text_peer_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The latest states a text peer was handed, newest first: a box that
    #: lost track of which one its disk holds (it restarted) sends its file
    #: as made on an unknown version, and these are the versions it is most
    #: likely made on (a write back alone misses what people typed since it).
    text_peer_views: Mapped[list[PeerView]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CrdtUpdate(Base):
    """One committed Loro delta: the canonical bytes the validator produced,
    never the bytes a client sent."""

    __tablename__ = "crdt_updates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "doc_type", "doc_id"],
            ["crdt_docs.org_id", "crdt_docs.doc_type", "crdt_docs.doc_id"],
            ondelete="CASCADE",
            name="fk_crdt_updates_doc",
        ),
        CheckConstraint("epoch >= 1", name="ck_crdt_updates_epoch_positive"),
        CheckConstraint("log_seq >= 1", name="ck_crdt_updates_log_seq_positive"),
        CheckConstraint("size >= 0", name="ck_crdt_updates_size_nonnegative"),
        CheckConstraint("octet_length(sha256) = 32", name="ck_crdt_updates_sha256_length"),
        Index("ix_crdt_updates_author_user_id", "author_user_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, primary_key=True)
    log_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: The client's idempotency key for the update this row came from.
    update_id: Mapped[str] = mapped_column(String(64), nullable=False)
    loro_peer: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_crdt_updates_author_user_id_users"),
        nullable=True,
    )
    #: The machine an agent wrote as, when one did.
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CrdtPeer(Base):
    __tablename__ = "crdt_peers"
    __table_args__ = (
        CheckConstraint(CRDT_DOC_TYPE_CHECK, name="ck_crdt_peers_doc_type"),
        CheckConstraint(
            f"loro_peer >= {CRDT_FIRST_CLIENT_PEER} AND loro_peer <= {CRDT_LAST_PEER}",
            name="ck_crdt_peers_loro_peer_range",
        ),
        Index("ix_crdt_peers_doc_user", "org_id", "doc_type", "doc_id", "user_id"),
        Index("ix_crdt_peers_user_id", "user_id"),
        Index("ix_crdt_peers_last_seen_at", "last_seen_at"),
    )

    loro_peer: Mapped[int] = mapped_column(
        BigInteger,
        crdt_peer_seq,
        primary_key=True,
        server_default=crdt_peer_seq.next_value(),
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_crdt_peers_org_id_teams"),
        nullable=False,
    )
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_crdt_peers_user_id_users"),
        nullable=False,
    )
    #: The socket holding this peer right now (``<instance>:<socket peer id>``).
    held_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "CRDT_DOC_TYPE_CHECK",
    "CRDT_FIRST_CLIENT_PEER",
    "CRDT_LAST_PEER",
    "CRDT_UNSAVED_PREDICATE",
    "STORED_DOC_TYPES",
    "CrdtDoc",
    "CrdtPeer",
    "CrdtUpdate",
    "crdt_peer_seq",
    "stored_doc_type",
]
