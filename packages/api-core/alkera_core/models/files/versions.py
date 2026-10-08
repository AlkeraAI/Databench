"""``file_versions`` — the immutable content chain behind a ``file`` node.

A version is never mutated: a write appends the next ``seq`` and moves the
node's ``head_version_id``, which is what makes an interrupted upload leave no
half-version behind. Bytes at or below the inline cap live in ``inline_bytes``
and never touch the store; anything larger is one whole object under
``store_key``, addressed inside the version's dedup domain.
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

#: Where a version's bytes came from. The value is what the history pane shows
#: and what the retention policy reasons about.
VERSION_SOURCES: tuple[str, ...] = (
    "upload",
    "box_export",
    "import",
    "restore",
    "document_snapshot",
    "rows",
    "copy",
)
#: Day one accepts JSON/CSV/text/images, sniffed server-side; everything else
#: stays ``pending`` and downloads as an attachment until the scanner lands.
SCAN_STATES: tuple[str, ...] = ("pending", "clean", "infected", "skipped", "failed")


class FileVersion(Base):
    __tablename__ = "file_versions"
    __table_args__ = (
        # Dense per node: the version list pages on it, and the uniqueness is
        # what makes a replayed commit land once rather than fork the chain.
        UniqueConstraint("node_id", "seq", name="uq_file_versions_node_seq"),
        # Serves the GC's reachability pass and the dedup lookup: every version
        # in an org that names a given content hash.
        Index("ix_file_versions_org_content_hash", "org_team_id", "content_hash"),
        # Serves the expiry janitor over non-head versions.
        Index(
            "ix_file_versions_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", name="fk_file_versions_node_id_file_nodes"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: BLAKE3 over the whole content: the identity a dedup lookup uses.
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 4 MiB-block SHA-256-of-SHA-256s, so a client can compute it itself and
    #: skip an upload without reading our bytes.
    block_hash: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    #: The chunk manifest, when the later chunking phase writes one. No foreign
    #: key yet: ``file_manifests`` is created by the second model group, and the
    #: constraint is added with it rather than dangling now.
    manifest_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: Content at or below the inline cap (64 KiB); NULL when it is in the store.
    inline_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: The object key inside the version's dedup domain; NULL when inline.
    store_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Sniffed server-side, never trusted from the client's Content-Type.
    mime_sniffed: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="application/octet-stream"
    )
    scan_state: Mapped[str] = mapped_column(
        Enum(
            *SCAN_STATES,
            name="ck_file_versions_scan_state",
            native_enum=False,
            length=16,
            create_constraint=True,
        ),
        nullable=False,
        server_default="pending",
    )
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(
        Enum(
            *VERSION_SOURCES,
            name="ck_file_versions_source",
            native_enum=False,
            length=32,
            create_constraint=True,
        ),
        nullable=False,
    )
    #: Exempt from version expiry, whatever the retention label says.
    keep_forever: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Under a legal hold: never purged, never expired.
    held: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The folder lease epoch this version was committed under, so a write from
    #: a stale mount is recognizable after the fact.
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Experimental fields, before one earns a typed column.
    version_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["SCAN_STATES", "VERSION_SOURCES", "FileVersion"]
