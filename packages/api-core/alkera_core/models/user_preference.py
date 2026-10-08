"""A user's stored preferences — the server's copy.

One row per user, holding the SAME shape the desktop keeps in
``~/.alkera/preferences.yml``: a whole
:class:`alkera_core.schemas.preferences.Preferences` document as JSONB. Storing
the document rather than a column per field is deliberate — ``Preferences`` is a
``VersionedModel`` with ``extra="allow"``, so a key a newer client writes
survives an older one's read, and a migration per preference would make every
new toggle an Alembic revision.

The row governs chats started in the BROWSER; the YAML governs the CLI and the
extension. Nothing syncs them: a sync needs a conflict rule, and a wrong one
silently overwrites the setting a person just chose on the other surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base

if TYPE_CHECKING:
    from alkera_core.models.user import User


class UserPreference(Base):
    __tablename__ = "user_preferences"

    # The user IS the key: exactly one preferences document per person, so a
    # concurrent write is an UPDATE rather than a second row nobody reads.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # The whole `Preferences` document, as the schema serializes it (including
    # its `schema_version`, which is what lets a future reader migrate it).
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship("User")
