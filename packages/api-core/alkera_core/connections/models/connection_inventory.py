"""Which integrations each member's workspace has connected and whether they
work. One row per member, per org, per workspace, per plugin, reported by the
member's own daemon under a credential for that org.

No credential material may land here, ever. A row carries the plugin, a health
status, and when it was last verified. It never carries attributes, credential
references, handles, or detail strings, because each of those can name
infrastructure or where a secret lives. Each report replaces one workspace's
rows, so a removed connection disappears instead of lingering as a phantom.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.account import dispositions
from alkera_core.account.dispositions import Disposition, Kind
from alkera_core.db.base import Base


class ConnectionInventoryEntry(Base):
    __tablename__ = "connection_inventory"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "org_team_id",
            "workspace_key",
            "plugin",
            name="uq_connection_inventory_user_org_workspace_plugin",
        ),
        # The previous release names this key in its upsert, so it stays until no
        # task of that release is left. Every row satisfies it while one identity
        # holds one org; it is dropped before an identity may hold a second.
        UniqueConstraint(
            "user_id",
            "workspace_key",
            "plugin",
            name="uq_connection_inventory_user_workspace_plugin",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The org the report was made in: the reporting credential's, stamped by
    #: the route, never a client field.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "teams.id", ondelete="CASCADE", name="fk_connection_inventory_org_team_id_teams"
        ),
        nullable=False,
        index=True,
    )
    #: Opaque digest of the reporting workspace's path and host. It scopes the
    #: replace, so two open workspaces never clobber each other.
    workspace_key: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The client's credential-health verdict. VARCHAR rather than an enum
    #: because the client owns the vocabulary and grows it ahead of the server.
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unverifiable")
    #: When the client last saw this connection healthy.
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the client last reported. Stale means quiet, not broken.
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# What erasing an account does to the columns here that name a person. Spelled
# beside the table so the two always load together.
dispositions.register(
    Disposition(
        "connection_inventory",
        "user_id",
        Kind.ERASE,
        "personal connector inventory",
        step=dispositions.delete_rows("connection_inventory", "user_id"),
    )
)
