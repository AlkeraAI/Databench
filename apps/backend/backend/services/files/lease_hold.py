"""Whether a box's beat may keep the folder it leases.

A box takes a chat's folder, or a workspace's, to run it, and keeps it by
beating. The beat proves the box is alive; it does not prove the box is still
the one meant to run the folder. Once a chat or a workspace has moved to
another box, the box it left may finish (its last push is the only copy of its
last turn) but may not stay: its beat forces the lease instead of extending
it, so the lease lapses within one TTL however long the old box keeps beating.
"""

from __future__ import annotations

from alkera_core.authz.principal import ActingContext
from alkera_core.compute.workspace_lease import holds_workspace
from alkera_core.files.authz.decider import WORKSPACE_SUBTYPE, AccessFacts
from alkera_core.models import WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.objects.workspaces import workspace_spec_of
from sqlalchemy.ext.asyncio import AsyncSession


async def beat_extends(
    db: AsyncSession, ctx: ActingContext, facts: AccessFacts, node: FileNode
) -> bool:
    """Whether the beat of the caller ``facts`` describes may extend its
    lease on ``node``; ``False`` forces it.

    Only a machine is asked: a box on its machine credential or on the worker
    credential it minted (the credential IS the proof, so the machine is the
    principal's own id), or an agent whose assertion was verified. Anyone
    else beats as they always have. A
    chat's folder is the box's while the chat is bound to it, which the
    caller's facts already say (``chats_bound_elsewhere``). A workspace's
    folder is the box's while the workspace's chats are not all on another box
    and, once the box has reported the workspace, while the box still holds it
    by the rule every door reads (:func:`~alkera_core.compute.workspace_lease.
    workspace_held_by`): a notebook-only sandbox the box reported keeps its
    lease, and a box the workspace's report was dropped from does not. A
    workspace no box has reported keeps the binding its chats give.

    Reads platform rows, so the caller runs it in its platform window.
    """
    machine = ctx.acting_principal.id if ctx.is_machine else facts.agent_machine_id
    if machine is None:
        return True
    if node.id in facts.chats_bound_elsewhere or node.id in facts.workspaces_bound_elsewhere:
        return False
    if node.subtype != WORKSPACE_SUBTYPE or node.target_object_id is None:
        return True
    workspace = await db.get(WorkspaceObject, node.target_object_id)
    if workspace is None:
        return False
    if workspace_spec_of(workspace.spec).binding_authority != "workspace":
        return workspace.deleted_at == 0
    return await holds_workspace(db, workspace_id=workspace.id, machine_id=machine)


__all__ = ["beat_extends"]
