"""Who counts as a member of the org a drive belongs to.

A person may belong to several orgs, so "a member of this org" is never read off
the person's row: it is an active row in ``org_memberships`` for that org. Every
Files read that names people (a grantee, a writer, a lease holder, a share
candidate) asks this one predicate.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, select

from alkera_core.models import MembershipStatus, OrgMembership
from alkera_core.models.user import User


def active_member_of(org_team_id: uuid.UUID) -> ColumnElement[bool]:
    """True for a ``users`` row in the statement that holds an active membership
    in ``org_team_id``."""
    return (
        select(OrgMembership.id)
        .where(
            OrgMembership.user_id == User.id,
            OrgMembership.org_team_id == org_team_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
        )
        .exists()
    )


__all__ = ["active_member_of"]
