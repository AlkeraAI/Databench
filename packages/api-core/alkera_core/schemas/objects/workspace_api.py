"""The HTTP shapes of ``/api/v1/workspaces``.

In-flight only, so plain ``BaseModel``s: the persisted half of a workspace is
:class:`~alkera_core.schemas.objects.specs.WorkspaceSpec`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field

from alkera_core.files.names import is_control
from alkera_core.schemas.objects.api import MAX_CLIENT_ID_LENGTH, MAX_TITLE_LENGTH
from alkera_core.schemas.objects.specs import (
    MachineStatus,
    MirrorState,
    SandboxState,
    WorkspaceKind,
    WorkspaceLayout,
)
from alkera_core.status import StatusFact

#: Characters that reorder or hide text: a title carrying them names a folder
#: that reads differently from what it is (``\u202egnp.exe`` shows as
#: ``exe.png``). They are removed, never kept.
_BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


#: What a title carrying a control character is refused with.
CONTROL_IN_TITLE = "A title may not contain control characters such as a tab, a line break or DEL."


def clean_title(value: str) -> str:
    """A title as it is stored and names a folder: bidi controls removed and
    surrounding whitespace trimmed. A title carrying a control character (a
    tab, a line break, DEL, the C1 range) is refused: it cannot show on the
    one line every surface draws a title on, and the folder it names would
    have to be repaired into another name. A title that is nothing once
    cleaned is refused."""
    if any(is_control(ord(ch)) for ch in value):
        raise ValueError(CONTROL_IN_TITLE)
    cleaned = _BIDI_CONTROLS.sub("", value).strip()
    if not cleaned:
        raise ValueError("A title needs at least one visible character.")
    return cleaned


WorkspaceTitle = Annotated[
    str, Field(min_length=1, max_length=MAX_TITLE_LENGTH), AfterValidator(clean_title)
]


class WorkspaceCreate(BaseModel):
    """A new project workspace, with a folder of its own."""

    title: WorkspaceTitle
    #: The client's own id, so a retried create lands on the same workspace.
    client_id: str | None = Field(default=None, min_length=1, max_length=MAX_CLIENT_ID_LENGTH)
    #: The org machine the workspace runs on ("Run on"), by id. Left out, the
    #: org's default machine for new workspaces when the creator may use it;
    #: sent as ``null``, the org's default placement whatever that default is.
    machine_pin: UUID | None = None


class WorkspaceUpdate(BaseModel):
    """A rename. ``expected_version`` is required: a write that does not say
    which row it read is a write that did not read one."""

    title: WorkspaceTitle
    expected_version: int


class WorkspaceRead(BaseModel):
    """One workspace as a list or a detail view shows it."""

    id: UUID
    title: str
    kind: WorkspaceKind
    layout: WorkspaceLayout
    owner_user_id: UUID
    version: int
    created_at: datetime
    updated_at: datetime
    #: The folder sharing acts on: share it through the Files permissions
    #: routes and every chat in the workspace is shared with it. A native
    #: workspace's own folder; for an adopted workspace of one, its chat's.
    #: ``None`` where Files is disabled or the folder is in the trash.
    files_node_id: UUID | None = None
    files_drive_id: UUID | None = None
    #: The shared working tree: ``files/`` in a native workspace, the chat's
    #: working directory in an adopted one.
    working_node_id: UUID | None = None
    #: The chat whose folder an adopted workspace points at.
    adopted_chat_id: UUID | None = None
    chat_count: int = Field(default=0, ge=0)
    #: How many of its chats are unread for THIS caller (see
    #: ``ChatSessionRead.unread``).
    unread_count: int = Field(default=0, ge=0)
    #: Where it runs, derived from its chats while each chat's binding is
    #: still the box's truth.
    machine_id: str | None = None
    machine_status: MachineStatus = "none"
    mirror_state: MirrorState | None = None
    wake_requested_at: datetime | None = None
    writable: bool = False
    #: The workspace's sandbox as the box running it last said: ``waking``,
    #: ``awake`` or ``asleep``. ``None`` until a box that runs a workspace's
    #: chats in one sandbox has served it; each chat still says its own state.
    sandbox_state: SandboxState | None = None
    #: What that sandbox held in memory at the box's last report, in MiB.
    sandbox_memory_used_mb: int | None = None
    #: Where the workspace stands as every surface draws it: its move, whether
    #: the box holding its folder still proves it syncs, the turns its chats
    #: are running, then where it was placed. Written here, word for word.
    #: ``None`` for a workspace nothing has happened in.
    status: StatusFact | None = None
    #: What THIS caller may do, decided by the policy the routes decide on.
    #: Hints for drawing controls, never grants.
    can_rename: bool = False
    can_delete: bool = False
    can_add_chat: bool = False


class WorkspaceList(BaseModel):
    items: list[WorkspaceRead]
    next_cursor: str | None = None


class WorkspaceDeletion(BaseModel):
    """Where a deleted workspace's deletion stands. The workspace and its chats
    are gone from every read the moment it is deleted; ``deleting`` means the
    background pass is still ending its chats and trashing their folders."""

    id: UUID
    state: Literal["deleting", "deleted"]
    chats_remaining: int = Field(ge=0)


__all__ = [
    "WorkspaceCreate",
    "WorkspaceDeletion",
    "WorkspaceList",
    "WorkspaceRead",
    "WorkspaceUpdate",
]
