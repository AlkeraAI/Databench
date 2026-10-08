"""A caller's own Files access facts, resolved without an HTTP request.

:func:`backend.api.deps.files_lane.facts_for` is the ROUTE spelling of this
function: it builds the request-cached role resolver and hands it here. A
background caller -- the Slack relay reading a produced image as the thread's
owner -- has no request and builds the resolver itself, but must arrive at the
same facts, or the same person would be decided differently depending on which
door asked. Spelled once, so the two cannot drift.

The reads here are platform tables (``team_memberships``, the team tree and the
chat rows behind the chat folders), so this runs OUTSIDE the Files transaction
-- the Files role is granted nothing but the Files tables.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from alkera_core.authz import RoleResolver
from alkera_core.authz.principal import ActingContext
from alkera_core.compute.machines import verify_machine_assertion
from alkera_core.files.authz.decider import CHAT_SUBTYPE, WORKSPACE_SUBTYPE, AccessFacts
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.workspace_object import WorkspaceObject
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services.org import ancestor_chain
from backend.services.sharing import admin_reads_private


async def caller_facts(db: AsyncSession, ctx: ActingContext, roles: RoleResolver) -> AccessFacts:
    """Every team ``ctx`` is in, the subset it administers, and whether it is an
    org admin.

    A team ACE on a node names one team id and the decider matches it against
    ``AccessFacts.team_ids`` literally, so the set has to be the caller's real
    memberships. ``team_memberships`` is materialized down every ancestor
    chain, so one read is the whole answer and an admin of a parent team
    arrives here already marked admin of the children. The read is scoped to
    the request's org (each membership row carries its root org), so a team the
    person holds in another of their orgs contributes no team id.

    Per-node facts -- ``leased_subtree``, the lock holder, the node flags -- are
    the caller's to fill in: they depend on the node, this does not.
    """
    if ctx.is_machine:
        # A box on its own credential is a member of no org and an admin of
        # nothing, whatever org the drive belongs to. It is held to the same
        # confinement as the box on its operator's session — an automated
        # principal that reaches a chat's folder only as the machine the chat
        # is bound to (:func:`verified_machine_id`) and never shares — so the
        # decider reads it as one; nothing of a person's reach rides along.
        return AccessFacts(is_agent=True)
    resolved = await roles.for_team(ctx.org_id)
    if not resolved.in_org:
        return AccessFacts(is_agent=ctx.is_agent)
    user_id = ctx.effective_user_id
    # The one setting that opens a member's private chat to an org admin opens
    # its folder too; read through the accessor every chat door reads, so the
    # folder and the chat cannot answer differently.
    reaches_chats = admin_reads_private()
    if user_id is None:
        return AccessFacts(
            org_admin=resolved.is_admin,
            org_admin_reaches_chats=reaches_chats,
            is_agent=ctx.is_agent,
        )
    rows = (
        await db.execute(
            select(TeamMembership.team_id, TeamMembership.role).where(
                TeamMembership.user_id == user_id, TeamMembership.org_team_id == ctx.org_id
            )
        )
    ).all()
    return AccessFacts(
        team_ids=frozenset(row.team_id for row in rows),
        team_admin_ids=frozenset(row.team_id for row in rows if row.role is TeamRole.ADMIN),
        org_admin=resolved.is_admin,
        org_admin_reaches_chats=reaches_chats,
        is_agent=ctx.is_agent,
    )


async def background_facts(db: AsyncSession, ctx: ActingContext) -> AccessFacts:
    """:func:`caller_facts` for a caller with no request to cache a role
    resolver on (a background decision, a socket's): it builds its own."""
    return await caller_facts(db, ctx, RoleResolver(db, ctx, ancestor_chain=ancestor_chain))


async def drive_confinement(
    db: AsyncSession, ctx: ActingContext, caller: AccessFacts, drive: FileDrive
) -> AccessFacts:
    """``caller`` (a caller's own facts, its proven machine filled in) with the
    confinement of an automated principal on ``drive``: the subtree it is held
    to and which chat and workspace folders of the drive run on its machine or
    on another. Every Files decision for an agent or a box on one drive is made
    on these, whichever door asks; a person's facts come back unchanged.

    The leases a request proves it holds ride its own headers, so they are the
    route's to add; a caller with no request (a socket) holds none.

    A person's own box (its credential names the owner) reaches no workspace
    folder and no chat folder but its owner's own chats', whichever door asks.
    """
    if not (ctx.is_agent or ctx.is_machine):
        return caller
    machine = caller.agent_machine_id
    confined = replace(caller, leased_subtree=drive.root_node_id)
    if machine is None:
        return confined
    owner = ctx.personal_owner_id
    org_id, drive_id = drive.org_team_id, drive.id
    here, elsewhere = await workspace_bindings(
        db, org_id=org_id, drive_id=drive_id, machine_id=machine, personal=owner is not None
    )
    return replace(
        confined,
        chats_bound_elsewhere=await chats_bound_elsewhere(
            db, org_id=org_id, drive_id=drive_id, machine_id=machine, personal_owner_id=owner
        ),
        chats_run_here=await chats_run_here(
            db, org_id=org_id, drive_id=drive_id, machine_id=machine, personal_owner_id=owner
        ),
        workspaces_run_here=here,
        workspaces_bound_elsewhere=elsewhere,
    )


async def verified_machine_id(db: AsyncSession, ctx: ActingContext) -> str | None:
    """The machine ``ctx`` PROVED it is on this request, or ``None``.

    An agent arrives asserting an id, and the id of the box serving a chat is
    public — it rides every chat row that box serves — so the assertion alone
    would let any member of the org act as the machine. It is checked against
    the registration instead (a live workspace machine of this org, registered
    by the user behind this request, on the very credential the request
    carries), and only a check that passes yields an id.

    Spelled once, here, because two answers to "which machine is this" would be
    two different fences: the access facts narrow an agent's actions on it, and
    the lease layer derives the holder it writes and compares from it. Only an
    agent pays for the read; everybody else is ``None`` without a statement.

    The read is of a platform table, so callers inside the Files transaction
    run it in their platform window.

    A box on its own machine credential asserted nothing: the credential IS
    the proof, checked at the door (unrevoked, its machine on the plane), and
    the machine it holds is the principal's id. No statement is paid for it.
    """
    if ctx.is_machine:
        return ctx.acting_principal.id
    if not ctx.is_agent:
        return None
    asserted = ctx.acting_principal.id
    verified = await verify_machine_assertion(
        db,
        machine_id=asserted,
        org_id=ctx.org_id,
        operator_user_id=ctx.effective_user_id,
        credential_id=ctx.credential_id,
    )
    return asserted if verified else None


async def chats_bound_elsewhere(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    drive_id: uuid.UUID,
    machine_id: str,
    personal_owner_id: uuid.UUID | None = None,
) -> frozenset[uuid.UUID]:
    """The chat folders of ``drive_id`` whose chat runs on a machine other than
    ``machine_id``.

    A box registers once and holds ONE credential for every chat it serves, so
    the assertion that proves it is a machine proves it on every chat folder in
    the org at once. What says which of them is the box's own is the chat row's
    own binding, and this is the read that resolves it: one statement over the
    drive the request addressed, answering the only question the decision turns
    on — which chat folders in reach belong to somebody else's box. A chat no
    box has taken yet is bound to nothing and is in nobody's answer, so it
    stays reachable by the first box that takes it.

    The chat object is a platform row, so this runs in the platform window
    :func:`backend.api.deps.files_lane.facts_for` already opens — and only
    for a request that proved it is a machine, since an agent that proved
    nothing has lost the actions this narrows already.

    A person's own box (``personal_owner_id``) is somebody else's box for
    every chat its person does not own, bound or not: no colleague's chat
    folder is ever its to take.
    """
    bound = WorkspaceObject.spec["machine_id"].astext
    elsewhere = and_(bound.is_not(None), bound != machine_id)
    if personal_owner_id is not None:
        elsewhere = or_(elsewhere, WorkspaceObject.owner_user_id != personal_owner_id)
    rows = (
        await db.execute(
            select(FileNode.id)
            .join(WorkspaceObject, WorkspaceObject.id == FileNode.target_object_id)
            .where(
                FileNode.drive_id == drive_id,
                FileNode.org_team_id == org_id,
                FileNode.subtype == CHAT_SUBTYPE,
                elsewhere,
            )
        )
    ).scalars()
    return frozenset(rows)


async def chats_run_here(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    drive_id: uuid.UUID,
    machine_id: str,
    personal_owner_id: uuid.UUID | None = None,
) -> frozenset[uuid.UUID]:
    """The chat folders of ``drive_id`` whose chat runs on ``machine_id``.

    The fact the box's own rung on a chat folder is decided from: a chat whose
    LIVE row is bound to this machine is the box's to run, and nothing else is.
    A chat bound to no machine is not in the answer — the binding is written
    when a box takes the chat, before the box touches its folder, so a folder
    with no binding is one no box has any business in; and neither is a folder
    whose chat row is gone or deleted (a restored folder of a deleted chat can
    never be bound, and used to read as "no machine yet"). A chat bound to some
    other machine is never in the answer, and neither is anything in another org
    or another drive. The binding is the field the chat's own doors admit the
    machine by, so the folder and the chat cannot disagree about which box runs
    it.

    Read only for a request that PROVED which machine it is — which already
    means a live workspace machine of this org, on the credential it registered
    with, that an admin of the org stands behind — in the caller's platform
    window, like the other machine facts. A person's own box runs only its
    person's chats, whatever is bound to it.
    """
    bound = WorkspaceObject.spec["machine_id"].astext
    stmt = (
        select(FileNode.id)
        .join(WorkspaceObject, WorkspaceObject.id == FileNode.target_object_id)
        .where(
            FileNode.drive_id == drive_id,
            FileNode.org_team_id == org_id,
            FileNode.subtype == CHAT_SUBTYPE,
            WorkspaceObject.deleted_at == 0,
            bound == machine_id,
        )
    )
    if personal_owner_id is not None:
        stmt = stmt.where(WorkspaceObject.owner_user_id == personal_owner_id)
    rows = (await db.execute(stmt)).scalars()
    return frozenset(rows)


async def workspace_bindings(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    drive_id: uuid.UUID,
    machine_id: str,
    personal: bool = False,
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    """``(run here, bound elsewhere)``: the native workspace folders of
    ``drive_id`` some live chat of which is bound to ``machine_id``, and those
    whose bound chats are all bound to other machines.

    A workspace runs on the box its chats are bound to: placement keeps them
    on one box, and the box leases the workspace's folder for all of them. A
    workspace none of whose chats is bound to anything is in neither answer,
    so the first box to serve one of its chats may take it, exactly as an
    unbound chat's folder may be taken. One statement for the drive, read only
    for a request that proved which machine it is.

    A person's own box (``personal``) never serves a workspace that owns a
    folder: every such folder in the drive is somebody else's to it.
    """
    if personal:
        folders = await db.execute(
            select(FileNode.id).where(
                FileNode.drive_id == drive_id,
                FileNode.org_team_id == org_id,
                FileNode.subtype == WORKSPACE_SUBTYPE,
            )
        )
        return frozenset(), frozenset(folders.scalars())
    chat = aliased(WorkspaceObject)
    bound = chat.spec["machine_id"].astext
    rows = (
        await db.execute(
            select(FileNode.id, bound)
            .join(WorkspaceObject, WorkspaceObject.id == FileNode.target_object_id)
            .join(
                chat,
                and_(
                    chat.spec["workspace_id"].astext == cast(WorkspaceObject.id, String),
                    chat.type == CHAT_SUBTYPE,
                    chat.deleted_at == 0,
                    chat.org_team_id == org_id,
                ),
            )
            .where(
                FileNode.drive_id == drive_id,
                FileNode.org_team_id == org_id,
                FileNode.subtype == WORKSPACE_SUBTYPE,
                WorkspaceObject.deleted_at == 0,
                bound.is_not(None),
                bound != "",
            )
        )
    ).all()
    here = {row[0] for row in rows if row[1] == machine_id}
    elsewhere = {row[0] for row in rows if row[1] != machine_id} - here
    return frozenset(here), frozenset(elsewhere)


__all__ = [
    "background_facts",
    "caller_facts",
    "chats_bound_elsewhere",
    "chats_run_here",
    "drive_confinement",
    "verified_machine_id",
    "workspace_bindings",
]
