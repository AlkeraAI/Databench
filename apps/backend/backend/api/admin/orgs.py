"""Admin: cross-tenant org operations.

Read + create open to ALKERA_SUPPORT (router-level floor). DELETE escalates
to ALKERA_ADMIN explicitly, and so does setting or clearing the org's storage
ceiling, through the ``platform.org_storage`` policy — reading it stays open to
support — so a support caller gets the policy's refusal with a decision row
rather than the router floor. The org's live-editing switch is decided the
same way, through ``platform.org_live_editing``.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.config import settings as app_settings
from alkera_core.email import send_email_verification
from alkera_core.models import PlatformRole, Team
from alkera_core.schemas.identity.user import UserRead
from alkera_core.schemas.tenancy.live_editing import OrgLiveEditingRead, OrgLiveEditingUpdate
from alkera_core.schemas.tenancy.org import (
    AdminOrgSettingsUpdate,
    OrgCreate,
    OrgCreateResponse,
    OrgRead,
    OrgSettingsRead,
    OrgUpdate,
)
from alkera_core.schemas.tenancy.storage import OrgStorageLimitUpdate, OrgStorageRead
from alkera_core.schemas.tenancy.team import TeamRead
from alkera_core.schemas.tenancy.team_membership import TeamMemberRead
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    require_platform_admin,
)
from backend.auth.password_policy import enforce_password
from backend.authz import enforce
from backend.services import org as org_services
from backend.services.audit import org_audit as org_audit_service
from backend.services.compute import ProvisionError
from backend.services.crdt import (
    live_editing_override,
    resolve_live_editing,
    write_back_org,
)
from backend.services.identity import UserConflictError
from backend.services.identity import email_verification as email_verification_service
from backend.services.org import memberships as membership_service
from backend.services.org import settings as org_settings_service
from backend.services.org import teams as team_service
from backend.services.realtime import crdt_lane

router = APIRouter(prefix="/orgs", route_class=AuditedRoute)


def _org_read(team: Team, member_count: int) -> OrgRead:
    return OrgRead.model_validate(team).model_copy(update={"member_count": member_count})


@router.get("", response_model=list[OrgRead])
async def list_orgs(db: DbSession) -> list[OrgRead]:
    rows = await team_service.list_all_roots(db)
    counts = await team_service.member_counts(db, [t.id for t in rows])
    return [_org_read(t, counts.get(t.id, 0)) for t in rows]


@router.post("", response_model=OrgCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_org(payload: OrgCreate, db: DbSession) -> OrgCreateResponse:
    enforce_password(
        payload.admin_password,
        email=payload.admin_email,
        names=(payload.admin_first_name, payload.admin_last_name),
    )
    try:
        org, admin = await team_service.create_org_with_admin(
            db,
            org_name=payload.name,
            admin_email=payload.admin_email,
            admin_first_name=payload.admin_first_name,
            admin_last_name=payload.admin_last_name,
            admin_password=payload.admin_password,
            # Always a regular Org Admin — platform staff is a separate ADMIN-gated
            # call, never settable through org bootstrap.
        )
    except UserConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Nobody has proven this address: staff typed it. The new admin gets the
    # same verification link a bare signup does, so the verify banner the
    # portal shows them is backed by a mail that was actually sent.
    token = await email_verification_service.issue_token(db, admin)
    await send_email_verification(admin, token=token)
    return OrgCreateResponse(
        org=OrgRead.model_validate(org),
        admin=UserRead.model_validate(admin),
    )


@router.get("/{org_id}", response_model=OrgRead)
async def get_org(org_id: UUID, db: DbSession, ctx: CurrentPrincipal) -> OrgRead:
    org = await _load_org(db, org_id)
    counts = await team_service.member_counts(db, [org.id])
    storage = await _storage_read(db, ctx, org_id)
    return _org_read(org, counts.get(org.id, 0)).model_copy(
        update={
            "storage_limit_bytes": storage.storage_limit_bytes,
            "storage_limit_source": storage.storage_limit_source,
            "storage_used_bytes": storage.storage_used_bytes,
        }
    )


async def _storage_read(db: AsyncSession, ctx: CurrentPrincipal, org_id: UUID) -> OrgStorageRead:
    drive = await org_services.org_drive(db, org_id)
    effective = await org_services.effective_org_limit(
        db, org_id, fallback_bytes=org_services.drive_default_bytes(drive)
    )
    override = await org_services.org_override(db, org_id)
    return OrgStorageRead(
        org_id=org_id,
        plan_tier=effective.plan,
        storage_limit_bytes=effective.limit_bytes,
        storage_limit_source=effective.source,
        storage_used_bytes=(
            0 if drive is None else await org_services.drive_used_bytes(db, ctx, drive)
        ),
        override_set=override is not None,
        updated_at=None if override is None else override.updated_at,
    )


async def _decide_storage(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    org_id: UUID,
    action: Action,
    operation: str,
) -> None:
    role = caller.platform_role
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.PLATFORM_ORG_STORAGE, id=str(org_id)),
        {
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "operation": operation,
        },
    )


@router.get("/{org_id}/storage", response_model=OrgStorageRead)
async def get_org_storage(
    org_id: UUID, request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> OrgStorageRead:
    """The org's effective ceiling, where it comes from, and what it has used."""
    await _decide_storage(
        request, db, ctx, caller, org_id=org_id, action=Action.READ, operation="read"
    )
    await _require_org(db, org_id)
    return await _storage_read(db, ctx, org_id)


@router.put("/{org_id}/storage", response_model=OrgStorageRead)
async def set_org_storage(
    org_id: UUID,
    payload: OrgStorageLimitUpdate,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> OrgStorageRead:
    """Set the org's ceiling by hand. ``limit_bytes: null`` is an explicit
    "unlimited"; the plan's figure no longer applies either way."""
    await _decide_storage(
        request, db, ctx, caller, org_id=org_id, action=Action.ADMIN, operation="set"
    )
    await _require_org(db, org_id)
    await org_services.set_org_override(db, org_id, limit_bytes=payload.limit_bytes, by=caller.id)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="storage.org_limit_set",
        acting=ctx,
        detail={"limit_bytes": payload.limit_bytes},
    )
    return await _storage_read(db, ctx, org_id)


@router.delete("/{org_id}/storage", response_model=OrgStorageRead)
async def clear_org_storage(
    org_id: UUID, request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> OrgStorageRead:
    """Back to the plan's figure. Idempotent."""
    await _decide_storage(
        request, db, ctx, caller, org_id=org_id, action=Action.ADMIN, operation="clear"
    )
    await _require_org(db, org_id)
    await org_services.clear_org_override(db, org_id)
    await org_audit_service.record(
        db, org_id=org_id, actor=caller, action="storage.org_limit_cleared", acting=ctx
    )
    return await _storage_read(db, ctx, org_id)


async def _decide_live_editing(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
    *,
    org_id: UUID,
    action: Action,
    operation: str,
) -> None:
    role = caller.platform_role
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.PLATFORM_ORG_LIVE_EDITING, id=str(org_id)),
        {
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "operation": operation,
        },
    )


async def _live_editing_read(db: AsyncSession, org_id: UUID) -> OrgLiveEditingRead:
    override = await live_editing_override(db, org_id)
    deployment = bool(app_settings.live_editing_enabled)
    return OrgLiveEditingRead(
        org_id=org_id,
        enabled=resolve_live_editing(deployment, override),
        override=override,
        deployment_default=deployment,
    )


@router.get("/{org_id}/live-editing", response_model=OrgLiveEditingRead)
async def get_org_live_editing(
    org_id: UUID, request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> OrgLiveEditingRead:
    """Whether the org's files open live, and whether that is its own setting."""
    await _decide_live_editing(
        request, db, ctx, caller, org_id=org_id, action=Action.READ, operation="read"
    )
    await _require_org(db, org_id)
    return await _live_editing_read(db, org_id)


@router.put("/{org_id}/live-editing", response_model=OrgLiveEditingRead)
async def set_org_live_editing(
    org_id: UUID,
    payload: OrgLiveEditingUpdate,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> OrgLiveEditingRead:
    """Set (``true``/``false``) or clear (``null``) the org's own switch.

    The setting commits first, so every replica refuses new live opens within
    a few seconds and ends open ones on their next tick. When the change
    leaves live editing off, every session of the org holding edits not yet on
    the drive is then written back before answering (bounded); what is left
    is the unsaved sweep's, and the answer says how many of each."""
    await _decide_live_editing(
        request,
        db,
        ctx,
        caller,
        org_id=org_id,
        action=Action.ADMIN,
        operation="clear" if payload.enabled is None else "set",
    )
    await _require_org(db, org_id)
    await org_settings_service.set_live_editing(db, org_id, payload.enabled)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action=(
            "live_editing.org_switch_cleared"
            if payload.enabled is None
            else "live_editing.org_switch_set"
        ),
        acting=ctx,
        detail={"enabled": payload.enabled},
    )
    read = await _live_editing_read(db, org_id)
    await db.commit()
    lane = crdt_lane(request.app)
    if lane is None:
        return read
    lane.switch.forget(org_id)
    if read.enabled:
        return read
    done = await write_back_org(lane, org_id)
    return read.model_copy(
        update={"sessions_written": done.written, "sessions_left_unsaved": done.left}
    )


@router.patch("/{org_id}", response_model=OrgRead)
async def rename_org(org_id: UUID, payload: OrgUpdate, db: DbSession) -> OrgRead:
    org = await _load_org(db, org_id)
    renamed = await team_service.rename(db, org, payload.name)
    counts = await team_service.member_counts(db, [renamed.id])
    return _org_read(renamed, counts.get(renamed.id, 0))


@router.get("/{org_id}/teams", response_model=list[TeamRead])
async def list_org_teams(org_id: UUID, db: DbSession) -> list[TeamRead]:
    """The org's full team tree (flat list w/ parent links + member_count) so
    the admin console can render the same TeamTree as the dashboard."""
    await _load_org(db, org_id)
    rows = await team_service.list_in_org(db, org_id)
    counts = await team_service.member_counts(db, [t.id for t in rows])
    return [
        TeamRead.model_validate(t).model_copy(update={"member_count": counts.get(t.id, 0)})
        for t in rows
    ]


@router.get("/{org_id}/members", response_model=list[TeamMemberRead])
async def list_org_members(org_id: UUID, db: DbSession) -> list[TeamMemberRead]:
    """Org-wide enriched members across every team in the org subtree."""
    await _load_org(db, org_id)
    team_ids = [t.id for t in await team_service.list_in_org(db, org_id)]
    rows = await membership_service.members_with_users(db, team_ids=team_ids, org_team_id=org_id)
    return [
        TeamMemberRead(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            role=membership.role,
            team_id=team_row.id,
            team_name=team_row.name,
            created_at=membership.created_at,
            direct_role=membership.role,
        )
        for membership, user, team_row in rows
    ]


async def _load_org(db: DbSession, org_id: UUID) -> Team:
    org = await team_service.get_by_id(db, org_id)
    if org is None or not org.is_root:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")
    return org


async def _require_org(db: DbSession, org_id: UUID) -> None:
    await _load_org(db, org_id)


@router.get("/{org_id}/settings", response_model=OrgSettingsRead)
async def get_org_settings(org_id: UUID, db: DbSession) -> OrgSettingsRead:
    await _require_org(db, org_id)
    settings = await org_settings_service.get_effective(db, org_id)
    return org_settings_service.read_shape(settings)


# ADMIN-only escalation — disabling a login provider can lock an entire tenant
# out of their accounts (OAuth-only users), so editing org login policy requires
# ALKERA_ADMIN, not just SUPPORT (mirrors the DELETE-org precedent).
@router.put(
    "/{org_id}/settings",
    response_model=OrgSettingsRead,
    dependencies=[Depends(require_platform_admin)],
)
async def update_org_settings(
    org_id: UUID, payload: AdminOrgSettingsUpdate, db: DbSession
) -> OrgSettingsRead:
    await _require_org(db, org_id)
    settings = await org_settings_service.apply_update(db, org_id, payload)
    return org_settings_service.read_shape(settings)


# ADMIN-only escalation — destructive.
@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_platform_admin)],
)
async def delete_org(org_id: UUID, db: DbSession) -> None:
    org = await team_service.get_by_id(db, org_id)
    if org is None or not org.is_root:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")
    try:
        await team_service.delete_org(db, org)
    except ProvisionError as exc:
        # A machine the provider would not confirm gone still holds the org's
        # data: nothing is purged, so a later attempt finds it again.
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from exc
