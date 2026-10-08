// How the rail sorts chats into workspaces.
//
// Main first, then the reader's projects, then what others shared with them.
// A workspace of one (a chat adopted into a workspace that is just its own
// folder) is drawn as the plain chat it is: with workspaces holding one chat
// each, a rail of one-chat workspaces would be the old list of chats with a
// chevron on every row. So only a workspace made as one (`layout: native`)
// gets a row of its own, and every other chat is listed as a chat.
//
// Membership is read off each chat's `workspace_id`; a chat whose workspace
// this reader cannot list (a chat shared on its own, or a row the adoption has
// not reached) is a plain chat too, never dropped.

import type { ChatSessionRead } from "../../../api/chats";
import type { WorkspaceRead } from "../../../api/workspaces";

export interface RailWorkspace {
  workspace: WorkspaceRead;
  chats: ChatSessionRead[];
}

export interface RailGroups {
  /** The reader's main workspace, when it is one this rail should show. */
  main: RailWorkspace | null;
  /** Project workspaces the reader owns. */
  projects: RailWorkspace[];
  /** Workspaces somebody else owns and shared with the reader. */
  shared: RailWorkspace[];
  /** The reader's own chats outside any workspace drawn as one. */
  chats: ChatSessionRead[];
  /** Somebody else's chats, shared on their own. */
  sharedChats: ChatSessionRead[];
}

export interface RailGroupInput {
  workspaces: readonly WorkspaceRead[] | undefined;
  chats: readonly ChatSessionRead[];
  userId: string | null;
  /** Whether a workspace may hold several chats on this server. With it off a
   *  main workspace can hold nothing, so an empty one is not drawn. */
  multiChat: boolean;
}

/** Whether a row is the reader's own. Which section of the rail it is drawn
 *  in, and nothing else: every control on it follows the server's `can_*`
 *  hints. Before the account answers, every row reads as the reader's own;
 *  a caller that must not guess (a workspace that might be someone else's
 *  main) checks for an unknown reader itself. */
export function ownedBy(row: { owner_user_id: string }, userId: string | null): boolean {
  return userId === null || row.owner_user_id === userId;
}

export function isMultiChatWorkspace(workspace: Pick<WorkspaceRead, "layout">): boolean {
  return workspace.layout === "native";
}

export function railGroups({ workspaces, chats, userId, multiChat }: RailGroupInput): RailGroups {
  const drawn = new Map<string, RailWorkspace>();
  for (const workspace of workspaces ?? []) {
    if (isMultiChatWorkspace(workspace)) drawn.set(workspace.id, { workspace, chats: [] });
  }
  const mine: ChatSessionRead[] = [];
  const others: ChatSessionRead[] = [];
  for (const chat of chats) {
    const holder = chat.workspace_id ? drawn.get(chat.workspace_id) : undefined;
    if (holder) {
      holder.chats.push(chat);
    } else if (!ownedBy(chat, userId)) {
      others.push(chat);
    } else {
      mine.push(chat);
    }
  }
  let main: RailWorkspace | null = null;
  const projects: RailWorkspace[] = [];
  const shared: RailWorkspace[] = [];
  for (const entry of drawn.values()) {
    const own = userId !== null && ownedBy(entry.workspace, userId);
    if (!own) {
      shared.push(entry);
    } else if (entry.workspace.kind === "main") {
      // An empty main workspace on a server where it can hold nothing is a
      // row with no use; one that holds chats is always shown.
      if (multiChat || entry.chats.length > 0) main = entry;
    } else {
      projects.push(entry);
    }
  }
  return { main, projects, shared, chats: mine, sharedChats: others };
}

/** The workspace a chat is drawn under, or `null` when it is drawn as a plain
 *  chat. */
export function railWorkspaceOf(groups: RailGroups, chatId: string | undefined): RailWorkspace | null {
  if (!chatId) return null;
  const all = [...(groups.main ? [groups.main] : []), ...groups.projects, ...groups.shared];
  return all.find((entry) => entry.chats.some((chat) => chat.id === chatId)) ?? null;
}
