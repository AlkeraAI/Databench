"""Workspaces: the object that holds chats sharing one file tree, and the
machine it runs on."""

from __future__ import annotations

from backend.services.workspaces.folders import WorkspaceNodes, nodes_for
from backend.services.workspaces.machine_move import (
    MoveRefusedError,
    cancel_move,
    default_machine_for,
    load_move,
    move_attrs,
    move_read,
    request_move,
    wake_held_for_choice,
)
from backend.services.workspaces.machine_move import Target as MoveTarget
from backend.services.workspaces.machine_move import admit as admit_move
from backend.services.workspaces.machine_move import machine_read as workspace_machine_read
from backend.services.workspaces.machine_move import stalled as move_stalled
from backend.services.workspaces.machine_move import start_workflow as start_move_workflow
from backend.services.workspaces.machine_move import target_for as move_target_for
from backend.services.workspaces.project_cap import PROJECT_CAP_REACHED, WorkspaceProjectCapError
from backend.services.workspaces.status import WorkspaceProgress
from backend.services.workspaces.status import gather as gather_progress
from backend.services.workspaces.status import status_fact as workspace_status_fact
from backend.services.workspaces.summary import ChatsSummary, summarize
from backend.services.workspaces.workspace_service import (
    WorkspaceClientIdTakenError,
    WorkspaceFolderMissingError,
    WorkspaceGoneError,
    adopt_chat,
    adopt_strays,
    chats_by_workspace,
    chats_in_statement,
    chats_page,
    connection_owner_id,
    create_project,
    default_target,
    end,
    ensure_main,
    find_main,
    folder_object_id,
    is_adopted,
    is_main,
    load,
    load_ended,
    may_write,
    multi_chat_workspace_of,
    placement_for,
    recent_chats,
    record_sandbox_report,
    refuses_chat_share,
    rename,
    set_chat_workspace,
    tombstone_adopted_for_chat,
)

__all__ = [
    "PROJECT_CAP_REACHED",
    "ChatsSummary",
    "MoveRefusedError",
    "MoveTarget",
    "WorkspaceClientIdTakenError",
    "WorkspaceFolderMissingError",
    "WorkspaceGoneError",
    "WorkspaceNodes",
    "WorkspaceProgress",
    "WorkspaceProjectCapError",
    "admit_move",
    "adopt_chat",
    "adopt_strays",
    "cancel_move",
    "chats_by_workspace",
    "chats_in_statement",
    "chats_page",
    "connection_owner_id",
    "create_project",
    "default_machine_for",
    "default_target",
    "end",
    "ensure_main",
    "find_main",
    "folder_object_id",
    "gather_progress",
    "is_adopted",
    "is_main",
    "load",
    "load_ended",
    "load_move",
    "may_write",
    "move_attrs",
    "move_read",
    "move_stalled",
    "move_target_for",
    "multi_chat_workspace_of",
    "nodes_for",
    "placement_for",
    "recent_chats",
    "record_sandbox_report",
    "refuses_chat_share",
    "rename",
    "request_move",
    "set_chat_workspace",
    "start_move_workflow",
    "summarize",
    "tombstone",
    "tombstone_adopted_for_chat",
    "wake_held_for_choice",
    "workspace_machine_read",
    "workspace_status_fact",
]
