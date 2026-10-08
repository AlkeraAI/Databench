"""Which workspace a chat is in, and which workspace holds a node.

The two answers live in different tables and would be easy to compare in
different id spaces. A chat names its workspace in its spec by the workspace
OBJECT's id; a node is held by the workspace FOLDER above it, a Files node
with an id of its own that stands for the object through its
``target_object_id``. This module is the one place either side is read, and
both come back as the workspace object's id, so a caller compares like with
like. Code that needs "is this chat's agent working where this node is" asks
:func:`chat_works_at` rather than comparing ids itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE, folder_object_kind
from alkera_core.models.files.tree import FileNode
from alkera_core.models.workspace_object import WorkspaceObject

#: The key a chat's spec names its workspace by: ``ChatSpec.workspace_id``, as
#: the row stores it. Read off the stored spec here, the one module that may,
#: so Files does not depend on the object schemas to answer where a chat is.
CHAT_WORKSPACE_KEY: Final = "workspace_id"


def _as_uuid(raw: object) -> uuid.UUID | None:
    if raw is None or raw == "":
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def chat_workspace_id(chat: WorkspaceObject) -> uuid.UUID | None:
    """The workspace object ``chat`` is in, as its spec names it, or ``None``
    for a chat in none (or one whose spec names something that is not an
    id)."""
    spec = chat.spec if isinstance(chat.spec, dict) else {}
    return _as_uuid(spec.get(CHAT_WORKSPACE_KEY))


def workspace_folder_on(path: Sequence[FileNode]) -> FileNode | None:
    """The innermost workspace folder on ``path`` (root first, the node
    itself last), or ``None`` when the path crosses none. Decided on the
    subtype the folder was minted with, so a folder whose workspace has ended
    is still found, and then stands for nothing."""
    for node in reversed(path):
        if node.subtype == WORKSPACE_TYPE:
            return node
    return None


def workspace_of_folder(folder: FileNode) -> uuid.UUID | None:
    """The workspace object the workspace folder ``folder`` stands for, or
    ``None`` when it stands for none (not a workspace folder, or one whose
    object was tombstoned off it)."""
    if folder.subtype != WORKSPACE_TYPE or folder_object_kind(folder) is None:
        return None
    return _as_uuid(folder.target_object_id)


def workspace_holding(path: Sequence[FileNode]) -> uuid.UUID | None:
    """The workspace object holding the last node of ``path``: the one the
    innermost workspace folder on it stands for. ``None`` outside every
    workspace folder, and inside one whose workspace has ended (an ended
    inner folder holds nobody, never its outer one's workspace)."""
    folder = workspace_folder_on(path)
    return None if folder is None else workspace_of_folder(folder)


def chat_works_at(chat: WorkspaceObject, path: Sequence[FileNode]) -> bool:
    """Whether ``chat``'s agent works where the last node of ``path`` is.

    Inside a workspace's folder: the chat is in that very workspace. Outside
    every workspace folder: the node is inside the chat's own folder (the
    node that stands for the chat, never one whose own id happens to be the
    chat's)."""
    folder = workspace_folder_on(path)
    if folder is not None:
        held_by = workspace_of_folder(folder)
        return held_by is not None and held_by == chat_workspace_id(chat)
    chat_id = _as_uuid(chat.id)
    return chat_id is not None and any(
        node.subtype == CHAT_TYPE and _as_uuid(node.target_object_id) == chat_id for node in path
    )


__all__ = [
    "chat_works_at",
    "chat_workspace_id",
    "workspace_folder_on",
    "workspace_holding",
    "workspace_of_folder",
]
