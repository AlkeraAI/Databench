"""What a person does to their own org memberships: found another org, leave one.

Both act for the signed-in identity on itself and never take an org from the
client. Founding names no existing org at all; leaving acts on the org the
caller's credential is in. Every route that offers either (the session routes
and the sign-in landing a person with no org reaches) calls these, so the
records each writes are the same whichever way the person came.

Each org's audit chain records only its own side: the new org records its
founding, the left org records the departure, and neither learns which other
orgs the person belongs to. The person's own security log records both.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.db.tenant_session import bound_org_ids
from alkera_core.models import OrgMembership, Team, User
from alkera_core.org_entitlements import org_entitlements
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import abuse as abuse_services
from backend.services import audit as audit_services
from backend.services import identity as identity_services
from backend.services.audit import ClientHint
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service

__all__ = ["OrgCreationLimitedError", "found_org", "leave_org", "next_org_after_leaving"]


class OrgCreationLimitedError(Exception):
    """The identity created as many orgs as the rolling window allows."""


async def _create_for_identity(
    db: AsyncSession, *, user: User, org_name: str, now: datetime
) -> Team:
    """Create an org founded by an EXISTING identity: no user row, the same org
    rows and founder standing a signup gets, an active membership, and a seat
    on the new org's billing. The seat goes through the convergence every seat
    takes, which grants the Free allowance only to an identity's first seat, so
    a created org never brings a second one.

    Refuses (:class:`OrgCreationLimitedError`) once the identity created the
    most orgs the rolling window allows. Concurrent creations by one identity
    are serialized, so two requests cannot both pass the same count."""
    await advisory_xact_lock(db, advisory_key("org-creation", user.id))
    if not await abuse_services.org_creation_allowed(db, user.id, now=now):
        raise OrgCreationLimitedError("too many organizations created recently")
    org = await team_service.create_org_rows(db, org_name=org_name)
    await org_membership_service.create(db, user_id=user.id, org_team_id=org.id)
    await team_service.seat_founder(db, org=org, founder_id=user.id, created_at=now)
    await org_entitlements().open_seat(db, user_id=user.id, org_id=org.id, now=now)
    return org


async def found_org(
    db: AsyncSession,
    *,
    user: User,
    name: str,
    now: datetime,
    client: ClientHint | None = None,
) -> Team:
    """Create an org founded by ``user`` (an existing identity) and put it on
    the record. Raises :class:`OrgCreationLimitedError` past the identity's
    creation cap."""
    if bound_org_ids(db) is not None:
        # A signed-in request is held to the org it is in, and the new org's
        # seats belong to an org that did not exist when the request bound.
        async with cross_tenant_write(db, reason="org.found_for_identity"):
            org = await _create_for_identity(db, user=user, org_name=name, now=now)
    else:
        org = await _create_for_identity(db, user=user, org_name=name, now=now)
    await audit_services.record_org_audit(
        db, org_id=org.id, actor=user, action="org.created", target=user.email
    )
    await audit_services.record_security_event(
        db, user_id=user.id, event="auth.org_created", org_team_id=org.id, client=client
    )
    return org


async def leave_org(
    db: AsyncSession,
    *,
    user: User,
    membership: OrgMembership,
    actor: Mapping[str, Any] | None,
    client: ClientHint | None = None,
) -> None:
    """Take ``user`` out of their membership's org for good.

    Their credentials in that org end, and nothing elsewhere changes (see
    :func:`org_memberships.remove`). Raises
    :class:`~backend.services.org.org_memberships.LastActiveAdminError` when
    they are its last active admin; the org's owner grant rides on the admin
    seat, so the last owner is its last admin too. The org's tree lock makes
    the last-admin check and the removal one step, so two admins leaving at
    once cannot both pass it."""
    org_id = membership.org_team_id
    await team_service.lock_org_tree(db, org_id)
    await org_membership_service.remove(db, membership, actor=actor)
    await audit_services.record_org_audit(
        db, org_id=org_id, actor=user, action="org.member_left", target=user.email
    )
    await audit_services.record_security_event(
        db, user_id=user.id, event="auth.org_left", org_team_id=org_id, client=client
    )


async def next_org_after_leaving(db: AsyncSession, user: User, *, left: UUID) -> UUID | None:
    """The org a client should switch to after ``user`` left ``left``: the most
    recently used org they can still enter, or None when they have none."""
    for view in await identity_services.switchable_orgs(db, user):
        if view.org_team_id != left:
            return view.org_team_id
    return None
