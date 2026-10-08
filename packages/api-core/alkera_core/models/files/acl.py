"""Who may see a node: interned ACLs, their exploded members, and shares.

``file_acls`` is **interned**: the body is content-addressed by ``body_hash``,
so a million nodes that share one permission set share one row and the listing
join is one row per distinct permission set rather than one per node. Interning
is ``ON CONFLICT DO NOTHING`` then read — never ``DO UPDATE``, which would make
the row a hot row and mutate the meaning of every node pointing at it.

``file_acl_members`` is the same body exploded into rows: it is the SQL side of
the ACL filter, so a listing or a search cuts its page *after* filtering instead
of authorizing row by row in Python.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: Who a grant can name. ``link`` is a separate kind precisely so an external
#: reader can never be reached by an ordinary user or team grant.
PRINCIPAL_KINDS: tuple[str, ...] = ("user", "team", "org", "link")


class FileAcl(Base):
    """An interned permission set, content-addressed by ``body_hash``."""

    __tablename__ = "file_acls"
    __table_args__ = (
        # The interning key: the writer hashes the canonical body and inserts
        # ON CONFLICT DO NOTHING, so identical sets collapse to one row.
        UniqueConstraint("body_hash", name="uq_file_acls_body_hash"),
        Index("ix_file_acls_org_team_id", "org_team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: ``[{principal_kind, principal_id, role, origin, expires_at, conditions}]``
    #: in canonical order. No deny entries and no cross-org entries exist, so
    #: "who can see this" is a chain read.
    body: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class FileAclMember(Base):
    """One row per principal in an ACL body: the join a listing filters on."""

    __tablename__ = "file_acl_members"
    __table_args__ = (
        # Serves the ACL filter inside every listing and search query: given the
        # caller's principal ids, which acl_ids grant them anything.
        Index("ix_file_acl_members_org_principal", "org_team_id", "principal_kind", "principal_id"),
    )

    acl_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_acls.id", ondelete="CASCADE", name="fk_file_acl_members_acl_id_file_acls"),
        primary_key=True,
    )
    principal_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: The strongest role this principal gets from the body.
    max_role: Mapped[str] = mapped_column(String(32), nullable=False)


class FileShare(Base):
    """A deliberate grant on one node, with an audit trail and an expiry."""

    __tablename__ = "file_shares"
    __table_args__ = (
        # Serves the item's sharing panel and the external-share inventory:
        # every live grant on a node.
        Index("ix_file_shares_node_id", "node_id"),
        # Serves "what has been shared with me" and the revoke sweep.
        Index("ix_file_shares_org_principal", "org_team_id", "principal_kind", "principal_id"),
        # One live grant per principal per node. A body takes the strongest of
        # the live rows, so a second one is access nobody can see and nobody can
        # withdraw: revoking the row a sharing panel is holding leaves the other
        # standing and the person still in. A withdrawn row is history and stays
        # outside the index, so the same person can be shared with again.
        Index(
            "uq_file_shares_live_principal",
            "node_id",
            "principal_kind",
            "principal_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("file_nodes.id", ondelete="CASCADE", name="fk_file_shares_node_id_file_nodes"),
        nullable=False,
    )
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Reserved for ABAC (device posture, network, time-of-day). Nullable from
    #: day one so the later phase adds values, not a migration on a hot table.
    conditions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["PRINCIPAL_KINDS", "FileAcl", "FileAclMember", "FileShare"]
