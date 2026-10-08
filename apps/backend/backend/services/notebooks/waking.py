"""Whether a notebook waits on a sleeping chat, and whether its wake is under way.

A notebook in a chat's folder, or in a workspace's, runs on the box that holds
that folder, and a box holds a chat's folder only while it serves the chat.
When every chat working there sleeps, no box holds the folder: a request for
its kernel either finds no holder at all (a chat that slept handed its lease
back) or reaches a box that no longer holds it (one that restarted). Either
way the notebook needs the same wake opening the chat asks for.

This module answers the facts that decision rests on, from the platform's own
reads and in the chat's own vocabulary (:func:`chat_session_state`, what the
chat page shows): the chats working where the notebook is
(:func:`notebook_chats`), whether every one of them sleeps on the machine that
would hold the folder (:func:`waits_on_wake`), and whether a chat's wake is
under way (:func:`is_waking`). Who may ask for the wake is the route's
question.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from alkera_core.files.objects_bridge import CHAT_TYPE
from alkera_core.files.workspace_identity import workspace_holding
from alkera_core.models import WorkspaceObject
from alkera_core.objects.chat_session_state import NO_BOX_WILL_OPEN, chat_session_state
from alkera_core.schemas.objects.specs import ChatSpec, MachineStatus, SessionState
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import workspaces
from backend.services.compute import live_machines
from backend.services.files import FolderHolder
from backend.services.notebooks.callers import Target

#: The session states under which a box holds the chat, and so its folder.
_UP: Final[frozenset[SessionState]] = frozenset({"awake", "working"})
#: The session states under which a box is coming for the chat.
_WAKING: Final[frozenset[SessionState]] = frozenset({"waking", "starting", "queued"})


async def notebook_chats(db: AsyncSession, target: Target) -> list[WorkspaceObject]:
    """The chats whose agents work where the notebook is, the most recently
    active first: the chats of the workspace holding it, or the chat whose own
    folder holds it. Empty for a notebook in no chat's folder (a plain drive
    folder, which only a person's own machine holds)."""
    path = [*target.allowed.chain, target.node]
    workspace_id = workspace_holding(path)
    if workspace_id is not None:
        return await workspaces.recent_chats(db, workspace_id)
    own = next((node for node in reversed(path) if node.subtype == CHAT_TYPE), None)
    if own is None or own.target_object_id is None:
        return []
    chat = await db.get(WorkspaceObject, uuid.UUID(str(own.target_object_id)))
    if (
        chat is None
        or chat.type != CHAT_TYPE
        or chat.deleted_at != 0
        or chat.org_team_id != target.org_id
    ):
        return []
    return [chat]


async def _standing(
    db: AsyncSession, org_id: uuid.UUID, specs: Sequence[ChatSpec]
) -> list[tuple[MachineStatus, SessionState]]:
    """Each chat's machine status and session state, as the chat page reads
    them."""
    machines = await live_machines(db, org_id=org_id)
    standing: list[tuple[MachineStatus, SessionState]] = []
    for spec in specs:
        status = machines.status_of(spec)
        standing.append((status, chat_session_state(spec, status, pending_turn=False)))
    return standing


async def waits_on_wake(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    chats: Sequence[WorkspaceObject],
    holder: FolderHolder | None,
) -> bool:
    """Whether the notebook's folder waits on one of ``chats`` waking: none
    of them is up, one of them is bound to a machine a box answers for (a
    chat no box will open, its box gone or refusing it, is the chat page's to
    sort out), and the folder is held by nobody or by the machine they are
    bound to. A folder held by some other machine (a person's own, syncing
    it) is served there, and a chat that is up means its box holds the
    folder: neither waits."""
    if not chats:
        return False
    specs = [ChatSpec.model_validate(chat.spec or {}) for chat in chats]
    if holder is not None and holder.machine_id not in {spec.machine_id for spec in specs}:
        return False
    standing = await _standing(db, org_id, specs)
    if any(state in _UP for _status, state in standing):
        return False
    return any(status not in NO_BOX_WILL_OPEN for status, _state in standing)


async def is_waking(db: AsyncSession, chat: WorkspaceObject) -> bool:
    """Whether a box is coming for ``chat`` now: a wake or a message waits
    for a box that can open it. Read fresh, after a wake was asked."""
    await db.refresh(chat)
    spec = ChatSpec.model_validate(chat.spec or {})
    ((_status, state),) = await _standing(db, chat.org_team_id, [spec])
    return state in _WAKING


__all__ = ["is_waking", "notebook_chats", "waits_on_wake"]
