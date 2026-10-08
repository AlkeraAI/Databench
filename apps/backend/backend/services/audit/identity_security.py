"""The identity security log: what happened to a person's account, in any org.

An identity may belong to several orgs, so the events that are the identity's
own are recorded here, for the person and platform staff: a sign-in, a password
change, reset or reset request, an email change, MFA turned on or off or a
backup code spent, a failed sign-in, a lockout, a logout or an ended session,
a refresh token presented again after its rotation (the session it belonged
to is ended),
an org deactivating, removing or re-roling the person, and a platform ban,
unban or disable. They are never in an org's audit chain: no org admin reads
what a person did elsewhere, and no org holds a person's account history.
What happens inside an org is recorded in that org's chain
(``org_audit_service.record``) by the caller, as well.

:func:`record` writes in the caller's transaction, like the org chain, so the
row commits or rolls back with the change it describes. A refused sign-in that
must outlive the request's rollback is written inside the caller's savepoint
and committed by the caller.

The log is kept for ``settings.identity_security_event_retention_days`` (a
worker job prunes what is older), except the platform's disable and re-enable
decisions, which org reactivation reads back as the platform's last word and
which therefore never age out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.models import IdentitySecurityEvent
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit.org_audit import scrub_detail


@dataclass(frozen=True, slots=True)
class ClientHint:
    """What the sessions list shows for a browser session. Display only: a
    client can say anything here, so nothing authorizes on it."""

    user_agent: str | None
    ip_prefix: str | None


#: The events this log carries. A fixed vocabulary: the self-read shows them
#: to the person, and a typo must not invent a new kind of event.
EVENTS: frozenset[str] = frozenset(
    {
        "auth.signed_in",
        "auth.password_changed",
        "auth.password_reset_requested",
        "auth.password_reset_completed",
        "auth.email_changed",
        "auth.mfa_enabled",
        "auth.mfa_disabled",
        "auth.mfa_backup_code_used",
        "auth.login_failed",
        "auth.locked_out",
        "auth.logout",
        "auth.sessions_revoked_all",
        "auth.session_revoked",
        "auth.refresh_token_reused",
        "auth.sso_enforced_signout",
        "auth.org_switched",
        "auth.org_joined",
        "auth.org_created",
        "auth.org_left",
        "auth.org_deactivated",
        "auth.org_reactivated",
        "auth.org_removed",
        "auth.org_role_changed",
        "auth.sso_linked",
        "platform.user_banned",
        "platform.user_ban_lifted",
        "platform.user_disabled",
        "platform.user_enabled",
        "account.export_requested",
        "account.export_downloaded",
        "account.deletion_requested",
        "account.deletion_cancelled",
    }
)

#: Ceiling on one page of the self-read.
MAX_PAGE = 100


async def record(
    db: AsyncSession,
    *,
    user_id: UUID,
    event: str,
    org_team_id: UUID | None = None,
    client: ClientHint | None = None,
    detail: dict[str, Any] | None = None,
) -> IdentitySecurityEvent:
    """Append one event to ``user_id``'s log, in the caller's transaction.

    ``org_team_id`` is the org the session was in, when there was a session;
    ``client`` is the coarse network and user agent the person sees
    (``session_issue.client_hint``). Raises ``ValueError`` for an event outside
    :data:`EVENTS`."""
    if event not in EVENTS:
        raise ValueError(f"{event!r} is not an identity security event")
    hint = client
    row = IdentitySecurityEvent(
        user_id=user_id,
        event=event,
        org_team_id=org_team_id,
        ip_prefix=hint.ip_prefix if hint is not None else None,
        user_agent=hint.user_agent if hint is not None else None,
        detail=scrub_detail(detail) if detail is not None else None,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


async def record_many(
    db: AsyncSession,
    *,
    user_ids: Iterable[UUID],
    event: str,
    org_team_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append the same event to each of ``user_ids``' logs with one flush: what
    an org did to many people at once (enforcing SSO ends every governed
    member's sessions). No client hint, because the people it happened to
    were not the ones asking. Returns how many rows it wrote."""
    if event not in EVENTS:
        raise ValueError(f"{event!r} is not an identity security event")
    scrubbed = scrub_detail(detail) if detail is not None else None
    now = datetime.now(UTC)
    rows = [
        IdentitySecurityEvent(
            user_id=user_id,
            event=event,
            org_team_id=org_team_id,
            detail=dict(scrubbed) if scrubbed is not None else None,
            created_at=now,
        )
        for user_id in dict.fromkeys(user_ids)
    ]
    db.add_all(rows)
    await db.flush()
    return len(rows)


async def list_for_user(
    db: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    before: datetime | None = None,
    before_id: UUID | None = None,
) -> Sequence[IdentitySecurityEvent]:
    """A page of ``user_id``'s own events, newest first. Never another
    identity's rows.

    The page is keyed on ``(created_at, id)``, the order it is read in: with
    ``before`` and ``before_id`` (the last row of the previous page) it holds
    exactly the rows after that one, so rows sharing the boundary timestamp
    are neither skipped nor repeated. ``before`` alone keeps its older
    meaning, strictly older than that time."""
    stmt = select(IdentitySecurityEvent).where(IdentitySecurityEvent.user_id == user_id)
    if before is not None and before_id is not None:
        stmt = stmt.where(
            or_(
                IdentitySecurityEvent.created_at < before,
                and_(
                    IdentitySecurityEvent.created_at == before,
                    IdentitySecurityEvent.id < before_id,
                ),
            )
        )
    elif before is not None:
        stmt = stmt.where(IdentitySecurityEvent.created_at < before)
    rows = await db.execute(
        stmt.order_by(
            IdentitySecurityEvent.created_at.desc(), IdentitySecurityEvent.id.desc()
        ).limit(max(1, min(limit, MAX_PAGE)))
    )
    return rows.scalars().all()


#: The names the ``backend.services.audit`` package exports for this module.
record_security_event = record
record_security_events = record_many
security_events_of = list_for_user
SECURITY_EVENTS = EVENTS
SECURITY_EVENTS_PAGE_MAX = MAX_PAGE

__all__ = ["EVENTS", "MAX_PAGE", "list_for_user", "record", "record_many"]
