"""The grant behind a rendered page and the files it reaches for.

A single signed content URL serves one version once, which is exactly wrong for
a page: an HTML report opened in a preview pulls the images, fonts and media
sitting beside it, and every one of those is a second request the browser makes
on its own. So a page is granted a *root* — the folder the entry file lives in
— for a short while, and each request under it is re-authorized against the
node it names.

The row is what makes that bounded. It carries the root and the entry the mint
decided, the user whose access every request under it resolves, and the very
credential that minted it, so a logout can close it; it counts the requests it
has served so a grant can never be a permanent read channel; and ``revoked_at``
lets a share revoke reach the grants rooted on the chain it changed. Nothing
about which files are reachable lives in the token: the token names the grant,
the grant names the root, and the walk from that root re-decides every node.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base


class FilePageGrant(Base):
    """One multi-use grant over a page's root folder."""

    __tablename__ = "file_page_grants"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_file_page_grants"),
        # Redemption reads by nonce alone, so the nonce carries its own unique
        # index rather than riding the primary key: the id is what the rest of
        # the system refers to a grant by, the nonce is the secret in the URL.
        UniqueConstraint("nonce", name="uq_file_page_grants_nonce"),
        # Serves the expiry sweep, which only ever looks at grants still live.
        Index(
            "ix_file_page_grants_expiry",
            "expires_at",
            postgresql_where=sql_text("revoked_at IS NULL"),
        ),
        # Serves a share revoke, which closes every grant rooted on the chain
        # whose permissions just changed.
        Index("ix_file_page_grants_root", "root_node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: The opaque secret inside the page URL.
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    #: The folder every path under this grant is resolved from. No foreign key:
    #: a node purged out from under a live grant must not keep the grant row
    #: alive, and the walk answers "not found" for a root that is gone anyway.
    root_node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: The file the grant was minted for — the page itself.
    entry_node_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: Whose access every request under this grant resolves. Never null: a
    #: grant that redeemed as nobody would be a read channel no policy covers.
    minted_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    #: The session credential the mint was made on, so a logout closes it.
    credential_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    requests_served: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sql_text("0"), default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["FilePageGrant"]
