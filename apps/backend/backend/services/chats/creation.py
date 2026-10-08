"""Creating a chat, and finding the one a retried create already made.

Every chat is created in a workspace in the same transaction: a named one, its
owner's main workspace once a workspace may hold several chats, or a workspace
of one that adopts the chat's folder (``backend.services.workspaces``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from alkera_core.authz.chat_scope import SCOPE_PRIVATE
from alkera_core.db.locking import LockRank, hold_place
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.schemas.objects import (
    NEW_CHAT_PERMISSION_MODE,
    ChatModelPin,
    ChatSpec,
    CloudPermissionMode,
    MachineStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import objects, workspaces


async def create_chat(
    db: AsyncSession,
    *,
    owner: User,
    org_id: UUID,
    title: str | None,
    client_id: str | None,
    machine_id: str | None,
    machine_status: MachineStatus,
    model: ChatModelPin | None = None,
    permission_mode: CloudPermissionMode = NEW_CHAT_PERMISSION_MODE,
    object_id: UUID | None = None,
    source_node_id: UUID | None = None,
    source_object_id: UUID | None = None,
    spare: bool = False,
    template_brief: str | None = None,
    workspace: WorkspaceObject | None = None,
) -> tuple[WorkspaceObject, bool]:
    """A new conversation, bound to whatever machine the placement resolver
    gave us, including none, which is a state the UI shows rather than an
    error the request fails with. It is filed in ``org_id``, the org of the
    request (or the Slack install) that started it.

    ``source_node_id`` / ``source_object_id`` record the replication context
    the chat was started from. They are written onto the spec rather than
    handed to the box per turn because the box that resumes the chat after a
    sleep is not the box that opened it, and a context that lived only in the
    running mirror would be gone the first time the chat slept.

    Raises :class:`ClientIdTakenError` when ``client_id`` already names an
    object of another type: the id space is per org and namespace, not per
    type, so the object store hands back whatever holds the id, and only a chat
    may be returned here as one.

    ``spare`` warms the chat ahead of its owner's first message: the row is
    written exactly like any other chat's (placed, pinned, given its folder)
    but flagged so every surface a person reads hides it until the owner's
    first send claims it (:mod:`alkera_core.objects.chat_spares`).

    ``template_brief`` is the template author's text, COPIED onto the chat at
    creation rather than read back from the template per turn. The box runs as
    the machine, and the template may be a folder only its author may read, so
    a box that fetched the brief itself would either be refused or be a way to
    read somebody's private template through a chat started from it.

    Every chat is created in a workspace, in the same transaction. ``workspace``
    names one (a native workspace the caller already decided may take it): the
    chat's folder is filed in its ``.chats``. Otherwise the chat goes in the
    owner's main workspace while ``workspaces_multi_chat`` is on, and in a
    workspace of one of its own while it is off (the box still runs one
    sandbox per chat). A workspace of one
    adopts the chat's folder rather than owning one, so the folder lands where
    a chat's always has.
    """
    target, parent_node_id = await workspaces.placement_for(
        db, owner=owner, org_id=org_id, workspace=workspace
    )
    spec = ChatSpec(
        machine_id=machine_id,
        machine_status=machine_status,
        last_seq=0,
        model=model,
        permission_mode=permission_mode,
        source_node_id=str(source_node_id) if source_node_id else None,
        source_object_id=str(source_object_id) if source_object_id else None,
        spare=spare,
        spare_active_at=datetime.now(UTC).isoformat() if spare else None,
        workspace_id=str(target.id) if target is not None else None,
    )
    body = spec.model_dump(mode="json")
    if template_brief is not None:
        body["metadata"] = {**dict(body.get("metadata") or {}), "template_brief": template_brief}
    # Private: a chat is its owner's alone until its node is shared, so the
    # object bridge projects no org-wide audience onto the node.
    chat, created = await objects.object_service.create_object(
        db,
        owner=owner,
        org_id=org_id,
        type="chat",
        title=title or "",
        spec=body,
        logical_id=client_id,
        object_id=object_id,
        visibility_scope=SCOPE_PRIVATE,
        parent_node_id=parent_node_id,
    )
    if not created and chat.type != "chat":
        raise ClientIdTakenError(client_id or "", held_by=chat.type)
    if not created and names_another_workspace(chat, workspace):
        raise ClientIdReplayError(client_id or "")
    if created and target is None:
        await workspaces.adopt_chat(db, chat=chat, owner=owner)
    if created:
        # Nobody else can see a chat this transaction has not committed: it
        # has no document a socket could hold, and its row is this
        # transaction's own from the insert. Both steps of a write to it are
        # done, so a caller that goes on to write to it in this same
        # transaction (a Slack mention that opens a thread and says its first
        # message) is not taking them after the folder locks its creation took.
        hold_place(db, LockRank.REALTIME_DOC, chat.id)
        hold_place(db, LockRank.WORKSPACE_OBJECT, chat.id)
    return chat, created


class ClientIdTakenError(ValueError):
    """A chat's ``client_id`` already names an object that is not a chat."""

    def __init__(self, client_id: str, *, held_by: str) -> None:
        super().__init__(f"client_id already names a {held_by}, not a chat")
        self.client_id = client_id
        self.held_by = held_by


class ClientIdReplayError(ClientIdTakenError):
    """A retried ``client_id`` names a workspace other than the one its chat is
    in: a different request reusing the id, not a retry."""

    def __init__(self, client_id: str) -> None:
        ValueError.__init__(self, "This client_id already started a chat in another workspace")
        self.client_id = client_id
        self.held_by = "chat"


def names_another_workspace(chat: WorkspaceObject, workspace: WorkspaceObject | None) -> bool:
    """Whether a request naming ``workspace`` would be handed ``chat`` from
    another one. A request naming no workspace never conflicts."""
    return workspace is not None and ChatSpec.model_validate(chat.spec or {}).workspace_id != str(
        workspace.id
    )


async def find_chat_by_client_id(
    db: AsyncSession, *, org_team_id: UUID, client_id: str
) -> WorkspaceObject | None:
    """The LIVE chat a retried ``client_id`` already made in this org, or ``None``.

    Scoped to ``type == "chat"``: the logical id space is shared with every
    other workspace object, and a saved query's id is not a chat's to answer
    for. The caller decides READ on what comes back before returning it.

    A deleted chat is not a chat anyone may be handed, so a tombstone answers
    ``None``, the same rule ``object_service.find_by_logical_id`` applies. The
    two lookups have to agree: this one decides what a retried create RETURNS,
    the object store decides whether the id may be RE-USED, and a tombstone the
    store refuses must not be handed back here as a live chat.
    """
    return (
        await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.org_team_id == org_team_id,
                WorkspaceObject.namespace == DEFAULT_NAMESPACE,
                WorkspaceObject.logical_id == client_id,
                WorkspaceObject.type == "chat",
                WorkspaceObject.deleted_at == 0,
            )
        )
    ).scalar_one_or_none()


__all__ = ["ClientIdTakenError", "create_chat", "find_chat_by_client_id"]
