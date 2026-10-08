"""An org admin adds a person by email: the deprecated ``POST /api/v1/users``.

The route predates multi-org accounts and let an admin create an identity with
a password the admin chose. It stays for the clients that script it, but it no
longer does either unsafe thing: an org never sets a person's password and
never reaches an identity that belongs to another org.

* An address with no identity anywhere gets a new identity WITHOUT a password,
  a seat on the org's root team (its home membership comes with the row), and
  the set-password email the person uses to choose their own.
* An address whose identity belongs to another org gets a pending invitation
  into this org, through the same path the invitation route takes, and the
  invitation email. The identity itself is not read back or touched.
* An address already in this org is a conflict, as it always was.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from alkera_core.auth.tenancy import home_org_id
from alkera_core.email import send_invitation_email, send_password_reset
from alkera_core.models import Invitation, Team, TeamRole, User
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import password_reset as password_reset_service
from backend.services.identity import users as user_service
from backend.services.identity.users import UserConflictError
from backend.services.org import add_member, create_invitation, membership_in


@dataclass(frozen=True, slots=True)
class AdminAdded:
    """What the request did: a new identity, or a pending invitation for an
    address that belongs to an identity elsewhere. Exactly one is set."""

    user: User | None = None
    invitation: Invitation | None = None


async def add_by_email(
    db: AsyncSession,
    *,
    admin: User,
    org_team_id: UUID,
    email: str,
    first_name: str,
    last_name: str,
    actor: Mapping[str, Any] | None,
) -> AdminAdded:
    """Add ``email`` to the org ``org_team_id`` as a member, safely (see the
    module docstring). Raises :class:`UserConflictError` when the address is
    already a member of the org, or is an identity whose home is this org."""
    normalized = user_service.normalize_email(email)
    existing = await user_service.get_by_email(db, normalized)
    if existing is None:
        user = await user_service.create_user(
            db,
            org_team_id=org_team_id,
            email=normalized,
            first_name=first_name,
            last_name=last_name,
            password=None,
        )
        await add_member(
            db, team_id=org_team_id, user_id=user.id, role=TeamRole.MEMBER, actor=actor
        )
        token = await password_reset_service.issue_token(db, user)
        await send_password_reset(user, token=token)
        return AdminAdded(user=user)

    if (
        await membership_in(db, user_id=existing.id, org_team_id=org_team_id) is not None
        or home_org_id(existing) == org_team_id
    ):
        raise UserConflictError(f"User with email {normalized!r} already exists")

    org = await db.get(Team, org_team_id)
    if org is None:  # pragma: no cover - the caller's credential names a live org
        raise UserConflictError("the organization no longer exists")
    invitation, _auto_accepted, raw_token = await create_invitation(
        db,
        team=org,
        email=normalized,
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=org_team_id,
        actor=actor,
    )
    await send_invitation_email(
        invitation,
        token=raw_token,
        team=org,
        org_name=org.name or "your organization",
        inviter_display_name=admin.display_name,
    )
    return AdminAdded(invitation=invitation)


__all__ = ["AdminAdded", "add_by_email"]
