"""The connections a box may use for what it holds: a chat bound to it, or a
workspace it reported holding.

A box acts for the owner without ever holding the owner's credential, so it
cannot ask the owner's own door. It asks on its own machine credential, per
chat or per workspace, and is answered what the owner's daemon would
materialize; a credential is a separate lease, decided again on the owner's
entitlement. The chat and workspace policies admit the box first, through the
same admission the open chat and workspace routes use.

Mounted by the connections extension behind the gate that admits a machine.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action
from alkera_core.connections.schemas import (
    MemberTeamConnectionsResponse,
    TeamConnectionCredentialLease,
)
from alkera_core.models import User
from fastapi import APIRouter, HTTPException, Request, status

from backend.api.deps.chat_access import decide_chat, load_chat
from backend.api.deps.connection_leases import owner_lease
from backend.api.deps.workspace_access import held_workspace
from backend.auth.dependencies import CurrentPrincipal, DbSession, PrincipalUser
from backend.services import sharing, workspaces
from backend.services.connections import member_connections

#: A chat's connections, for the box the chat is bound to.
chats_router = APIRouter(prefix="/api/v1/chats", tags=["chats"])
#: A workspace's connections, for the box that holds it.
workspaces_router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


@workspaces_router.get("/{workspace_id}/connections", response_model=MemberTeamConnectionsResponse)
async def workspace_connections(
    request: Request,
    workspace_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> MemberTeamConnectionsResponse:
    """The connections attached to a workspace, for the box that holds it: the
    names its notebook kernels and its chats may use.

    Sharing a workspace shares every connection its owner may use, so this is
    the owner's whole set: team connections, the owner's personal ones and
    per-user ones (used with the owner's own credential). The answer is the
    one the owner's own daemon would materialize. Decided on the workspace:
    the box that reported holding it is admitted on its own machine
    credential; every other machine, and every person, is told the workspace
    does not exist (a person reads their connections on their own door). No
    secret rides this answer; a lease is a separate door.
    """
    workspace, owner = await held_workspace(
        request, db, ctx, user, workspace_id, Action.LIST_CONNECTIONS
    )
    listed = await member_connections(db, owner, org_id=workspace.org_team_id)
    await db.commit()
    return listed


@workspaces_router.post(
    "/{workspace_id}/connections/{connection_id}/credential-lease",
    response_model=TeamConnectionCredentialLease,
)
async def lease_workspace_connection_credential(
    request: Request,
    workspace_id: UUID,
    connection_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> TeamConnectionCredentialLease:
    """One of the workspace owner's connections, leased to the box that holds
    the workspace for that workspace alone: a shared row's bundle, or a
    per-user row's OWNER grant. Decided twice and on record twice: the
    workspace policy admits only the holding box, then the connector policy
    decides the connection on the owner's entitlement with the binding
    compared against the box again. Written to the org's audit chain in the
    owner's name with the machine's chain and the workspace it was for."""
    workspace, owner = await held_workspace(
        request, db, ctx, user, workspace_id, Action.LEASE_CONNECTION_CREDENTIAL
    )
    lease = await owner_lease(
        request,
        db,
        ctx,
        org_id=workspace.org_team_id,
        bound_machine_id=await sharing.workspace_machine_id(db, workspace),
        owner=owner,
        connection_id=connection_id,
        workspace_id=str(workspace.id),
    )
    await db.commit()
    return lease


@chats_router.get("/{chat_id}/connections", response_model=MemberTeamConnectionsResponse)
async def chat_connections(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: PrincipalUser
) -> MemberTeamConnectionsResponse:
    """The connections the agent running this chat may use, for the box that
    runs it.

    A chat on a shared machine acts for its owner without the box ever holding
    the owner's credential, so the box cannot ask ``/me/team-connections`` — a
    person's door, answered about the caller. It asks here instead, per chat,
    on its own machine credential, and is answered the set the OWNER's own
    daemon would materialize (the same builder as ``/me/team-connections``,
    resolved server-side from the chat's owner). Decided on the chat: the box
    the chat is bound to is admitted, every other machine and every person is
    told the chat does not exist. The secrets themselves never ride this
    answer; a lease is a separate door.

    The owner here is the WORKSPACE's: every chat in a workspace uses its
    owner's connections, so a chat a collaborator started in somebody else's
    workspace is answered the workspace owner's set, never the collaborator's.
    For a workspace of one that is the chat's own owner.
    """
    chat = await load_chat(db, chat_id)
    await decide_chat(request, db, ctx, user, chat, Action.LIST_CONNECTIONS)
    owner = await db.get(User, await workspaces.connection_owner_id(db, chat))
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await member_connections(db, owner, org_id=chat.org_team_id)


@chats_router.post(
    "/{chat_id}/connections/{connection_id}/credential-lease",
    response_model=TeamConnectionCredentialLease,
)
async def lease_chat_connection_credential(
    request: Request,
    chat_id: UUID,
    connection_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> TeamConnectionCredentialLease:
    """One connection's shared credential bundle, leased to the box that runs
    this chat, for the chat's owner.

    The listing above hands the box the names; this hands it a secret, so it is
    decided twice and on record twice. The chat policy admits the box the chat
    is bound to and nobody else — every other machine and every person, the
    owner included, is told the chat does not exist. The connector policy then
    decides the connection on the OWNER's entitlement, exactly as the owner's
    own daemon would be decided on ``/me/team-connections/{id}/credential-lease``,
    with the chat's binding compared against the box a second time. The bundle
    is time-bounded like the person's lease and the box keeps it in memory
    only; the org's audit chain records the lease in the owner's name with the
    machine's chain and the chat it was for.

    Leased for the workspace owner, by the same rule as the listing above.
    """
    chat = await load_chat(db, chat_id)
    attrs = await decide_chat(request, db, ctx, user, chat, Action.LEASE_CONNECTION_CREDENTIAL)
    owner = await db.get(User, await workspaces.connection_owner_id(db, chat))
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await owner_lease(
        request,
        db,
        ctx,
        org_id=chat.org_team_id,
        bound_machine_id=str(attrs["bound_machine_id"]),
        owner=owner,
        connection_id=connection_id,
        chat_id=str(chat.id),
    )
