"""IdentitySecurityEvent: the security log of one identity, across every org.

An identity authenticates once and may belong to several orgs, so what happens
to the identity itself (a password change or reset, an email change, MFA turned
on or off, a failed sign-in, a lockout, a logout, a platform ban or disable)
belongs to the person and to platform staff, never to an org's audit chain: an
org admin must not read what a person did in another org, and an org must not
be the place a person's own account history lives.

What happens inside an org (entering it, an SSO refusal for it, an action in
it) is written to that org's chain (``org_audit_events``) as well; the row here
names the org the session was in, when there was one, so the person sees where
it happened.

``ip_prefix`` is the coarse network (a /24 or /48), the same shape the sessions
list shows, never a full address. ``detail`` is small and scrubbed of secrets
before write.

The log is kept for a retention window (a worker job prunes older rows),
except :data:`PLATFORM_DECISION_EVENTS`: whether the platform last disabled or
re-enabled an identity is read back from here, so those rows never age out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: The platform's disable and re-enable of an identity. The newest of the two is
#: the platform's standing decision (an org's reactivation reads it before it
#: may lift an old offboarding), so the retention prune never removes them.
PLATFORM_DECISION_EVENTS: frozenset[str] = frozenset(
    {"platform.user_disabled", "platform.user_enabled"}
)


class IdentitySecurityEvent(Base):
    __tablename__ = "identity_security_events"
    __table_args__ = (
        # The one read: a person's own events, newest first.
        Index("ix_identity_security_events_user_created", "user_id", text("created_at DESC")),
        # The retention prune: the oldest rows, across every identity.
        Index("ix_identity_security_events_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_identity_security_events_user"),
        nullable=False,
    )
    # A stable dotted code, e.g. "auth.password_changed", "auth.login_failed".
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    # The org the session was in when it happened; NULL for an event with no
    # session (a failed sign-in, a reset link) or one the platform did.
    org_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="SET NULL", name="fk_identity_security_events_org"),
        nullable=True,
    )
    ip_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
