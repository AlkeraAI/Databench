"""The HTTP side of handing a connection's credential out.

The lease service (``backend.services.connections``) resolves the facts, asks
for the decision and hands the bundle or the owner's grant out. Here its
decision call is bound to the request (``enforce``, which records the decision
and raises the refusal) and its refusals become the status codes every door
answers with: 404 for anything not handed out, 409 when the stored credential
cannot be opened or the owner must sign in again, 422 when the owner's
credential is on their own computer, 503 when the provider is unreachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource
from alkera_core.connections.models import TeamConnection
from alkera_core.connections.schemas import TeamConnectionCredentialLease
from alkera_core.models import User
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz import enforce
from backend.services.connections import (
    PROVIDER_UNREACHABLE_CODE,
    PROVIDER_UNREACHABLE_MESSAGE,
    ConnectionNotFoundError,
    CredentialUnreadableError,
    Decide,
    OwnerCredentialOnDeviceError,
    OwnerReauthRequiredError,
    ProviderUnreachableError,
    decide_credential_fetch,
    lease_for_owner,
    shared_bundle,
)


def decider(request: Request, db: AsyncSession, ctx: ActingContext) -> Decide:
    """The service's decision call, bound to this request."""

    async def decide(action: Action, resource: Resource, attrs: Mapping[str, object]) -> object:
        return await enforce(request, db, ctx, action, resource, attrs)

    return decide


def lease_refusal(exc: Exception) -> HTTPException | None:
    """The response a lease refusal answers with; ``None`` for anything else."""
    if isinstance(exc, ConnectionNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    if isinstance(exc, CredentialUnreadableError):
        return HTTPException(
            status.HTTP_409_CONFLICT, detail={"code": "credential_unreadable", "message": str(exc)}
        )
    if isinstance(exc, OwnerReauthRequiredError):
        return HTTPException(
            status.HTTP_409_CONFLICT, detail={"code": "owner_reauth_required", "message": str(exc)}
        )
    if isinstance(exc, OwnerCredentialOnDeviceError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "owner_credential_on_device", "message": str(exc)},
        )
    if isinstance(exc, ProviderUnreachableError):
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": PROVIDER_UNREACHABLE_CODE, "message": PROVIDER_UNREACHABLE_MESSAGE},
        )
    return None


_REFUSALS = (
    ConnectionNotFoundError,
    CredentialUnreadableError,
    OwnerReauthRequiredError,
    OwnerCredentialOnDeviceError,
    ProviderUnreachableError,
)


async def member_bundle(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    connection_id: UUID,
    *,
    user: User,
    action: str,
) -> tuple[TeamConnection, str, dict[str, str]]:
    """The person's door: the shared bundle the caller is entitled to, in their
    credential's org."""
    try:
        conn = await decide_credential_fetch(
            db, decider(request, db, ctx), connection_id, for_user=user, org_id=ctx.org_id
        )
        secret, named_secrets = await shared_bundle(
            db, ctx, conn, for_user=user, org_id=ctx.org_id, action=action
        )
    except _REFUSALS as exc:
        raise lease_refusal(exc) or exc from None
    return conn, secret, named_secrets


async def owner_lease(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    *,
    org_id: UUID,
    bound_machine_id: str,
    owner: User,
    connection_id: UUID,
    chat_id: str = "",
    workspace_id: str = "",
) -> TeamConnectionCredentialLease:
    """The box's door for a chat or a workspace: one connection the owner may
    use, as a lease."""
    try:
        return await lease_for_owner(
            db,
            decider(request, db, ctx),
            ctx,
            org_id=org_id,
            bound_machine_id=bound_machine_id,
            owner=owner,
            connection_id=connection_id,
            chat_id=chat_id,
            workspace_id=workspace_id,
        )
    except _REFUSALS as exc:
        raise lease_refusal(exc) or exc from None
