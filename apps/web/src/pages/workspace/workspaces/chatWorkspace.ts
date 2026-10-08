// What a chat knows about the workspace it sits in, and where its files are.

import type { ChatSessionRead } from "../../../api/chats";
import type { WorkspaceRead } from "../../../api/workspaces";

export const MAIN_WORKSPACE_TITLE = "Main";
/** What a workspace is called on screen. A main workspace is "Main" whatever
 *  its stored title. */
export function workspaceTitle(workspace: Pick<WorkspaceRead, "kind" | "title">): string {
  if (workspace.kind === "main") return MAIN_WORKSPACE_TITLE;
  return workspace.title.trim() || "Untitled workspace";
}

/** The workspace fields a chat read carries from a server that runs every chat
 *  of a workspace in one shared tree. Optional throughout: an older server
 *  sends none of them. */
export type ChatWorkspaceFields = Pick<
  ChatSessionRead,
  "workspace_layout" | "workspace_files_node_id"
>;

/** Where the chat's Files tab is rooted: the workspace's shared `files/` tree
 *  when the chat works in one (so a file dropped there reaches the agent), else
 *  the chat's own working directory, else the chat's folder. */
export function chatFilesRoot(
  chat: Pick<ChatSessionRead, "files_node_id"> & ChatWorkspaceFields,
  chatWorkingNodeId: string | null | undefined,
): string | null {
  return chat.workspace_files_node_id || chatWorkingNodeId || chat.files_node_id || null;
}
