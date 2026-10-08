"""Which chats a box serves as members of one workspace, and how.

A chat in a workspace that owns a folder (``workspace_layout == "native"``) is
served by the box as a member of that workspace: the box holds ONE lease on
the workspace's ``.alkeraworkspace`` folder, every member chat runs with the
workspace's shared ``files/`` tree as its home, and each chat keeps its own
records folder (under ``.chats/``) on a lease of its own nested beneath the
workspace's. A chat in a workspace of one, or with no workspace at all, is
served exactly as before: its own folder, its own lease, its own working
directory. The row decides, through :func:`seat_of`; nothing else in the box
guesses.

Pure: a chat row in, a :class:`WorkspaceSeat` (or a refusal, or nothing) out,
and the names a box keys its custody of a workspace by.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from alkera_core.files.objects_bridge import (
    WORKSPACE_CHATS_FOLDER,
    WORKSPACE_TYPE,
    working_folder_name,
)

#: The prefix a workspace's custody key carries in the tables a box keys by
#: chat id (the folder custody, the beat, the locks): it can never collide
#: with a chat id, which is a UUID, and it reads as what it is in a log line.
WORKSPACE_KEY_PREFIX: Final = "ws:"

#: The workspace folder's shared tree and its records folder, as the drive
#: names them: read from the bridge that mints them, so the box and the drive
#: cannot name them differently.
_WORKSPACE_WORKING = working_folder_name(WORKSPACE_TYPE)
if _WORKSPACE_WORKING is None:
    raise RuntimeError("a workspace folder is born with its shared tree")
FILES_DIR: Final = _WORKSPACE_WORKING.decode("utf-8")
CHATS_DIR: Final = WORKSPACE_CHATS_FOLDER.decode("utf-8")


def workspace_key(workspace_id: str) -> str:
    """The custody key of a workspace: the id under :data:`WORKSPACE_KEY_PREFIX`."""
    return f"{WORKSPACE_KEY_PREFIX}{workspace_id}"


def is_workspace_key(key: str) -> bool:
    return key.startswith(WORKSPACE_KEY_PREFIX)


def workspace_of_key(key: str) -> str | None:
    """The workspace id inside a custody key, or ``None`` for a chat's key."""
    return key[len(WORKSPACE_KEY_PREFIX) :] if is_workspace_key(key) else None


@dataclass(frozen=True, slots=True)
class WorkspaceSeat:
    """Where a member chat sits: the workspace's folder (what the box leases)
    and its shared tree (what the chat runs in), on one drive."""

    workspace_id: str
    node_id: str
    files_node_id: str
    drive_id: str

    @property
    def key(self) -> str:
        return workspace_key(self.workspace_id)

    def custody_row(self) -> dict[str, str]:
        """The workspace folder as the folder custody reads a chat row: the
        node it leases and the drive it is on, under the keys every chat row
        names its own folder by."""
        return {"files_node_id": self.node_id, "files_drive_id": self.drive_id}


@dataclass(frozen=True, slots=True)
class SeatRefused:
    """A chat that says it is in a native workspace, whose folder the box
    cannot serve it from: the reason is what the chat's banner says."""

    reason: str


#: What a chat whose records left its workspace's folder is told. Moving a
#: chat between workspaces is not supported yet; the server refuses the move,
#: so this is a row written before that refusal existed or a folder restored
#: somewhere else.
MOVED_OUT: Final = (
    "this chat's folder is no longer inside its workspace's folder; "
    "moving a chat between workspaces is not supported yet"
)


def _text(row: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def seat_of(chat: Mapping[str, Any]) -> WorkspaceSeat | SeatRefused | None:
    """How a box serves ``chat``, read off its row.

    ``None``: as a chat on its own, exactly as a box always has. That is a
    workspace of one (``adopted``), a row from a backend that says nothing
    about workspaces, and any row that does not say ``native``. A
    :class:`WorkspaceSeat`: as a member of the workspace the row names. A
    :class:`SeatRefused`: a native workspace's chat whose folder the row says
    is no longer inside the workspace's folder (no node ids), which a box must
    not serve from either folder: its own would split it from its siblings,
    and the workspace's is not where its records are.
    """
    if _text(chat, "workspace_layout", "workspaceLayout") != "native":
        return None
    workspace_id = _text(chat, "workspace_id", "workspaceId")
    node_id = _text(chat, "workspace_node_id", "workspaceNodeId")
    files_node_id = _text(chat, "workspace_files_node_id", "workspaceFilesNodeId")
    drive_id = _text(chat, "files_drive_id", "filesDriveId")
    if workspace_id is None:
        return None
    if node_id is None or files_node_id is None or drive_id is None:
        return SeatRefused(MOVED_OUT)
    return WorkspaceSeat(
        workspace_id=workspace_id, node_id=node_id, files_node_id=files_node_id, drive_id=drive_id
    )


__all__ = [
    "CHATS_DIR",
    "FILES_DIR",
    "MOVED_OUT",
    "WORKSPACE_KEY_PREFIX",
    "SeatRefused",
    "WorkspaceSeat",
    "is_workspace_key",
    "seat_of",
    "workspace_key",
    "workspace_of_key",
]
