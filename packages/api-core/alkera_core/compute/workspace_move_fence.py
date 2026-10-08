"""Ending what a departed box still holds of a moved workspace.

At the switch a box that still answers keeps its leases on the workspace's
folders and hands them back itself, with the push of its last turn. A box
whose hand-back never comes leaves the target waiting on a lease nobody will
give up until it lapses: its worker ended without handing anything back, or a
process the stopped turn left running in the workspace's sandbox keeps the
box holding the shared tree. The target cannot take the workspace, the move
times out and the chats go back where they came from.

So once the move has waited :data:`HAND_BACK_SECONDS` in ``waking`` without
the target taking the workspace, every lease a box other than the target
still holds on the workspace's folder, its chats' folders or anything under
them is ended here, the same fence a box that stopped answering gets at the
switch. The box, if it is still there, finds its lease gone on its next beat
and lets the folder go. What it pushed before is on the drive; a box that
answers a flush pushed everything before the switch.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.logging import get_logger
from alkera_core.models import WorkspaceObject
from alkera_core.models.org_machines import WorkspaceMachineMove

log = get_logger(__name__)

#: How long a departed box gets, from the move reaching ``waking``, to hand
#: back what it holds of the workspace itself before its leases are ended.
HAND_BACK_SECONDS: Final = 60


def hand_back_overdue(move: WorkspaceMachineMove, now: datetime) -> bool:
    """Whether the departed boxes have had their time to hand back: the move
    has been ``waking`` (its last state change) for :data:`HAND_BACK_SECONDS`."""
    return now - move.updated_at >= timedelta(seconds=HAND_BACK_SECONDS)


async def fence_departed(
    db: AsyncSession,
    move: WorkspaceMachineMove,
    chats: Sequence[WorkspaceObject],
    *,
    target: str,
) -> list[uuid.UUID]:
    """End every live lease a machine other than ``target`` holds on the
    workspace's folder, its chats' folders or anything under them. Returns the
    nodes whose lease ended (none on a second call)."""
    roots = [move.workspace_id, *(chat.id for chat in chats)]
    rows = (
        await db.execute(
            text(
                "UPDATE file_leases l SET released_at = now(), grantable_after = now() "
                "FROM file_nodes n "
                "WHERE n.id = l.node_id AND l.org_team_id = :org "
                "AND l.holder_kind = 'machine' AND l.machine_id <> :target "
                "AND l.released_at IS NULL AND l.reaped_at IS NULL AND l.expires_at > now() "
                "AND EXISTS (SELECT 1 FROM file_nodes r WHERE r.org_team_id = :org "
                "AND r.target_object_id = ANY(CAST(:roots AS uuid[])) AND r.kind = 'folder' "
                "AND r.subtype IN (:workspace, :chat) AND n.path_ids <@ r.path_ids) "
                "RETURNING l.node_id, l.machine_id"
            ),
            {
                "org": move.org_team_id,
                "target": target,
                "roots": [str(root) for root in roots],
                "workspace": WORKSPACE_TYPE,
                "chat": CHAT_TYPE,
            },
        )
    ).all()
    if rows:
        log.warning(
            "workspace.machine_move.departed_fenced",
            move_id=str(move.id),
            workspace_id=str(move.workspace_id),
            machines=sorted({str(row.machine_id) for row in rows}),
            folders=len(rows),
        )
    return [uuid.UUID(str(row.node_id)) for row in rows]


__all__ = ["HAND_BACK_SECONDS", "fence_departed", "hand_back_overdue"]
