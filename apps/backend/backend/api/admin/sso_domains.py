"""Admin: the email domains an org's single sign-on speaks for.

A domain decides where its people's sign-in goes and which addresses an org's
IdP and SCIM may create, so it is assigned by platform staff and never by the
org: an org admin who could type a domain could claim another company's
people. Reading is open to all staff; assigning and removing is platform admin
only, through the ``platform.org_sso_domains`` policy, so every attempt lands a
decision row. Each domain added or removed is also recorded on the org's own
audit trail, naming the staff member.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.auth import sso_domains
from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.models import PlatformRole
from alkera_core.schemas.identity.sso import SsoDomainsRead, SsoDomainsUpdate
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.admin._audit import AuditedRoute
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services.audit import record_org_audit
from backend.services.identity import (
    PublicMailboxDomainError,
    UnknownOrgError,
    assign_sso_domains,
    sso_domains_of_org,
)

router = APIRouter(prefix="/orgs", route_class=AuditedRoute)


async def _decide(
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
        Resource(ResourceType.PLATFORM_ORG_SSO_DOMAINS, id=str(org_id)),
        {
            "platform_staff": role is not None,
            "platform_admin": role is PlatformRole.ALKERA_ADMIN,
            "operation": operation,
        },
    )


def _no_org() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")


@router.get("/{org_id}/sso/domains", response_model=SsoDomainsRead)
async def get_sso_domains(
    org_id: UUID, request: Request, db: DbSession, ctx: CurrentPrincipal, caller: CurrentUser
) -> SsoDomainsRead:
    await _decide(request, db, ctx, caller, org_id=org_id, action=Action.READ, operation="read")
    try:
        domains = await sso_domains_of_org(db, org_id)
    except UnknownOrgError as exc:
        raise _no_org() from exc
    return SsoDomainsRead(org_id=org_id, domains=list(domains))


@router.put("/{org_id}/sso/domains", response_model=SsoDomainsRead)
async def set_sso_domains(
    org_id: UUID,
    payload: SsoDomainsUpdate,
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    caller: CurrentUser,
) -> SsoDomainsRead:
    """Replace the org's SSO email domains with ``domains``.

    400 for a value that is not a domain or is a public mailbox provider's;
    409 ``sso_domain_held`` when another org holds one (which org is not
    said). Nothing changes on a refusal. An org left with no domain stops
    requiring SSO, since its IdP could then sign nobody in."""
    await _decide(request, db, ctx, caller, org_id=org_id, action=Action.ADMIN, operation="set")
    try:
        change = await assign_sso_domains(db, org_id, payload.domains, assigned_by=caller.id)
    except UnknownOrgError as exc:
        raise _no_org() from exc
    except sso_domains.InvalidDomainError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Not an email domain: {exc.value}"
        ) from exc
    except PublicMailboxDomainError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{exc.domain} is a public mailbox domain and cannot be assigned to an org.",
        ) from exc
    except sso_domains.DomainHeldError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": sso_domains.DOMAIN_HELD_CODE,
                "message": f"{exc.domain} is already assigned to another organization.",
            },
        ) from exc
    for action, domains in (
        ("sso.domain_assigned", change.added),
        ("sso.domain_removed", change.removed),
    ):
        for domain in domains:
            await record_org_audit(
                db, org_id=org_id, actor=caller, action=action, target=domain, acting=ctx
            )
    if change.enforcement_disabled:
        await record_org_audit(
            db,
            org_id=org_id,
            actor=caller,
            action="sso.enforcement_disabled",
            detail={"reason": "no_domain_assigned"},
            acting=ctx,
        )
    return SsoDomainsRead(org_id=org_id, domains=list(await sso_domains_of_org(db, org_id)))
