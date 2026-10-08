"""Which box holds a workspace's folder, read off its live ``workspace`` lease.

The lease is the one fact that says where a shared workspace runs: every chat
of a workspace that owns a folder runs in one sandbox under that lease, and
while one box holds it every other box is refused the folder. Placement reads
it to decide where a chat goes; the workspace's own doors read it to decide
whether the box asking still holds the workspace.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    CompoundSelect,
    Select,
    String,
    and_,
    cast,
    exists,
    func,
    or_,
    select,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.models import FileLease, FileNode, WorkspaceObject

#: The lease a box takes on a shared workspace's folder to run its chats.
WORKSPACE_LEASE_PURPOSE = "workspace"


async def workspace_lease_holder(
    db: AsyncSession, *, org_team_id: UUID, workspace_id: UUID
) -> UUID | None:
    """The machine holding the live ``workspace`` lease on the folder of
    workspace ``workspace_id``, or ``None``. That box is where the workspace
    runs: every other box is refused the folder (and so every chat of it)
    until the lease ends."""
    stmt = (
        select(FileLease.holder_principal_id)
        .join(FileNode, FileNode.id == FileLease.node_id)
        .where(
            FileLease.org_team_id == org_team_id,
            FileLease.holder_kind == "machine",
            FileLease.purpose == WORKSPACE_LEASE_PURPOSE,
            FileLease.released_at.is_(None),
            FileLease.reaped_at.is_(None),
            FileLease.expires_at > func.now(),
            FileNode.org_team_id == org_team_id,
            FileNode.target_object_id == workspace_id,
            FileNode.subtype == WORKSPACE_TYPE,
            FileNode.kind == "folder",
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def workspace_held_by(workspace: Any, machine_id: ColumnElement[str] | str) -> ColumnElement[bool]:
    """THE rule for whether box ``machine_id`` holds ``workspace`` NOW, as SQL
    over the workspace row (``WorkspaceObject`` or an alias of it).

    The box must have reported the workspace itself (``binding_authority ==
    "workspace"`` naming it): only a box that reports its workspace runs that
    workspace's kernel sandbox, and a binding derived from chats is not proof
    that a box holds it. The report alone is not enough either, because it
    outlives the hold: nothing rewrites it when the workspace's chats move or
    are deleted. So the box must still hold the workspace by one of the two
    facts that end with the hold, a live chat of the workspace bound to it or
    the live ``workspace`` lease on its folder (a sandbox kept up for a
    notebook kernel after its chats are gone), and no live chat of the
    workspace may be bound to another box: the workspace goes where its chats
    go, and the box they left is finishing a hand-back, not serving it.

    One clause, so every reader asks the same question: the workspace's doors
    (its connections and their credentials), the orgs a pool box's session may
    read, and whether a box's beat may keep the workspace's lease.
    """
    on_box = aliased(WorkspaceObject)
    elsewhere = aliased(WorkspaceObject)

    def _chats_of(chat: Any) -> ColumnElement[bool]:
        return and_(
            chat.org_team_id == workspace.org_team_id,
            chat.type == CHAT_TYPE,
            chat.deleted_at == 0,
            chat.spec["workspace_id"].astext == cast(workspace.id, String),
        )

    bound_here = on_box.spec["machine_id"].astext
    bound_there = elsewhere.spec["machine_id"].astext
    leased = (
        select(FileLease.node_id)
        .join(FileNode, FileNode.id == FileLease.node_id)
        .where(
            FileLease.org_team_id == workspace.org_team_id,
            FileLease.holder_kind == "machine",
            FileLease.purpose == WORKSPACE_LEASE_PURPOSE,
            FileLease.released_at.is_(None),
            FileLease.reaped_at.is_(None),
            FileLease.expires_at > func.now(),
            cast(FileLease.holder_principal_id, String) == machine_id,
            FileNode.org_team_id == workspace.org_team_id,
            FileNode.target_object_id == workspace.id,
            FileNode.subtype == WORKSPACE_TYPE,
            FileNode.kind == "folder",
        )
    )
    return and_(
        workspace.type == WORKSPACE_TYPE,
        workspace.deleted_at == 0,
        workspace.spec["binding_authority"].astext == "workspace",
        workspace.spec["machine_id"].astext == machine_id,
        or_(
            exists(select(on_box.id).where(_chats_of(on_box), bound_here == machine_id)),
            exists(leased),
        ),
        ~exists(
            select(elsewhere.id).where(
                _chats_of(elsewhere),
                bound_there.is_not(None),
                bound_there != "",
                bound_there != machine_id,
            )
        ),
    )


def held_orgs_query(machine_id: str) -> CompoundSelect[tuple[UUID]]:
    """The orgs whose live chats are bound to ``machine_id`` or whose
    workspaces it still holds (:func:`workspace_held_by`, the rule the
    workspace's own doors read, so a box's session is never wider than what
    those doors admit it to). A report the
    box left behind when the workspace's chats moved on adds nothing. Two
    arms, each matching the predicate of its own partial index
    (``ix_workspace_objects_chat_machine`` and
    ``ix_workspace_objects_workspace_machine``), so both are indexed reads."""
    chats = select(WorkspaceObject.org_team_id).where(
        WorkspaceObject.type == "chat",
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["machine_id"].astext == machine_id,
    )
    workspaces = select(WorkspaceObject.org_team_id).where(
        WorkspaceObject.type == "workspace",
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["binding_authority"].astext == "workspace",
        WorkspaceObject.spec["machine_id"].astext == machine_id,
        workspace_held_by(WorkspaceObject, machine_id),
    )
    return union(chats, workspaces)


def holds_work_in_query(machine_id: str, org_id: UUID) -> Select[tuple[bool]]:
    """Whether box ``machine_id`` has any standing in ``org_id`` now, as one
    statement: it holds work there by :func:`held_orgs_query` (a live chat
    bound to it, a workspace it still holds), or it is finishing on a live
    Files lease of the org that it holds (the hand-back of a folder a chat
    moved off it, which the Files decider lets the holder complete until the
    lease ends). Every arm is an index probe.

    A pool box may be placed in any org, so "serves" is no standing at all:
    this is the question a box's credential is held to in an org other than
    its own. Outside it, the org's rows read the same as rows that do not
    exist."""
    held = held_orgs_query(machine_id).subquery()
    arms = [exists(select(held.c[0]).where(held.c[0] == org_id))]
    holder = _uuid_or_none(machine_id)
    if holder is not None:
        arms.append(
            exists(
                select(FileLease.node_id).where(
                    FileLease.org_team_id == org_id,
                    FileLease.holder_kind == "machine",
                    FileLease.holder_principal_id == holder,
                    FileLease.released_at.is_(None),
                    FileLease.reaped_at.is_(None),
                    FileLease.expires_at > func.now(),
                )
            )
        )
    return select(or_(*arms))


async def holds_work_in(db: AsyncSession, *, machine_id: str, org_id: UUID) -> bool:
    """:func:`holds_work_in_query`, asked."""
    return bool((await db.execute(holds_work_in_query(machine_id, org_id))).scalar_one())


def _uuid_or_none(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except ValueError:
        return None


async def holds_workspace(db: AsyncSession, *, workspace_id: UUID, machine_id: str) -> bool:
    """:func:`workspace_held_by` for one workspace and one box."""
    found = await db.execute(
        select(WorkspaceObject.id).where(
            WorkspaceObject.id == workspace_id,
            workspace_held_by(WorkspaceObject, machine_id),
        )
    )
    return found.first() is not None


__all__ = [
    "WORKSPACE_LEASE_PURPOSE",
    "held_orgs_query",
    "holds_work_in",
    "holds_work_in_query",
    "holds_workspace",
    "workspace_held_by",
    "workspace_lease_holder",
]
