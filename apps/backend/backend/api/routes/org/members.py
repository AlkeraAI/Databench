"""Org-admin member management: list members + deprovision (deactivate/reactivate).

Deactivation is the offboarding lever, and it acts on the person's membership
in THIS org: every credential they hold here is refused from the next request
on, their live chats here end, and they cannot sign in to this org again until
reactivated. Their identity and every other org they belong to are untouched.
It is fully reversible (their data and team seats stay). The last ACTIVE admin
cannot be deactivated, and an admin cannot deactivate themselves, so an org can
never be locked out of its own administration.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.models import OrgMembership, User
from alkera_core.schemas.identity.org_members import OrgMemberRead, SetMemberActiveRequest
from fastapi import APIRouter, HTTPException, status

from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    DbSession,
    OrgAdmin,
    OrgAdminVerified,
)
from backend.services.audit import org_audit as org_audit_service
from backend.services.identity import users as user_service
from backend.services.org import (
    LastActiveAdminError,
    deactivate_membership,
    members_of,
    membership_in,
    org_admin_ids,
    reactivate_membership,
)

router = APIRouter(prefix="/api/v1/org/members", tags=["org-members"])


def _read(user: User, membership: OrgMembership, *, admin_ids: set[UUID]) -> OrgMemberRead:
    return OrgMemberRead(
        user_id=user.id,
        email=user.email,
        display_name=membership.display_name or user.display_name,
        is_admin=user.id in admin_ids,
        # Active only when the person can actually act here: the org's own
        # decision (the membership) and the platform's (the identity) both.
        is_active=membership.is_active and user.is_active,
        sso_exempt=membership.sso_exempt,
    )


@router.get("", response_model=list[OrgMemberRead])
async def list_org_members(
    db: DbSession, _admin: OrgAdmin, org_id: CurrentOrg
) -> list[OrgMemberRead]:
    rows = await members_of(db, org_id)
    admin_ids = set(await org_admin_ids(db, org_id=org_id))
    return [_read(u, m, admin_ids=admin_ids) for u, m in rows]


@router.put("/{user_id}/active", response_model=OrgMemberRead)
async def set_member_active(
    user_id: UUID,
    payload: SetMemberActiveRequest,
    db: DbSession,
    admin: OrgAdminVerified,
    org_id: CurrentOrg,
    ctx: CurrentPrincipal,
) -> OrgMemberRead:
    membership = await membership_in(db, user_id=user_id, org_team_id=org_id)
    user = await user_service.get_by_id(db, user_id) if membership is not None else None
    if membership is None or user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found in your organization"
        )
    if payload.active:
        await reactivate_membership(db, membership, actor=ctx.audit_dict())
    else:
        if user.id == admin.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate yourself"
            )
        try:
            await deactivate_membership(db, membership, actor=ctx.audit_dict())
        except LastActiveAdminError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last active admin",
            ) from exc
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="member.deactivated" if not payload.active else "member.reactivated",
        target=user.email,
    )
    admin_ids = set(await org_admin_ids(db, org_id=org_id))
    return _read(user, membership, admin_ids=admin_ids)


__all__ = ["router"]
