"""Who may act on a person's account: the person on their own browser
session, or platform staff through the support tool.

Shared by the account lifecycle's routes and by any extension's routes on the
same account resource (the data export), so each decides through ``enforce()``
the same way.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.models import User
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.account_authority import account_standing
from backend.authz import enforce


async def acting_directly(
    db: AsyncSession, request: Request, ctx: ActingContext, user: User
) -> bool:
    """Whether the caller is the person on their own browser session: a user
    principal on a session JWT, no agent in the chain, and a browser sign-in
    behind it (a CLI token is a JWT too, but no browser session)."""
    principal = ctx.acting_principal
    if ctx.is_agent or principal.kind is not PrincipalKind.USER:
        return False
    if principal.credential is not CredentialKind.JWT:
        return False
    return (await account_standing(db, request, user)).browser_session


async def authorize_self(
    request: Request, db: AsyncSession, ctx: ActingContext, user: User, action: Action
) -> None:
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.ACCOUNT, id=str(user.id)),
        {
            "is_self": True,
            "acting_directly": await acting_directly(db, request, ctx, user),
            "platform_role": user.platform_role.value if user.platform_role else "",
        },
    )


async def authorize_account(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    staff: User,
    target: User,
    action: Action,
) -> None:
    """Decide ``staff``'s ``action`` on ``target``'s account requests."""
    await enforce(
        request,
        db,
        ctx,
        action,
        Resource(ResourceType.ACCOUNT, id=str(target.id)),
        {
            "is_self": staff.id == target.id,
            "acting_directly": await acting_directly(db, request, ctx, staff),
            "platform_role": staff.platform_role.value if staff.platform_role else "",
        },
    )


async def account_target(db: AsyncSession, user_id: UUID) -> User:
    """The person a support route names, or 404."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return target


__all__ = ["account_target", "acting_directly", "authorize_account", "authorize_self"]
