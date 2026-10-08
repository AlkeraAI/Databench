// What kinds of tab a chat's workspace can hold.
//
// The tabs a person leaves open are stored on the server and handed back the
// next time they open the chat, so the set of kinds a build understands and the
// set a stored document contains are NOT the same set. A `switch` over kinds
// would force the reader to decide what to do with a kind it has never heard of
// at the moment it reads it, and the easy answer — drop it — is a silent loss
// the person only notices on the client that could still open it.
//
// So kinds register themselves, an unknown one is KEPT and rendered with a
// sentence saying this build is behind, and a lane adding a kind adds a
// registration rather than editing the tab strip.

import type { ComponentType } from "react";

import type { Item } from "@/api/files";
import type { LiveSocket } from "@/api/realtime/crdt/channel";

/** One stored tab, exactly as the workspace document carries it. Ids only: a
 *  tab names a node, never its bytes, its path on a machine or its content. */
export interface WorkspaceTab {
  id: string;
  kind: string;
  node_id?: string | null;
  name: string;
  path?: string | null;
  params?: Record<string, unknown>;
  /** The editor group the tab is in. */
  group?: string;
  /** How a file tab shows its file (see `fileViewers`). Absent: the default. */
  view?: string;
  /** A preview tab: italic, and replaced by the next file opened the same way. */
  transient?: boolean;
}

/** What a tab's renderer is handed: the chat it belongs to and the folder its
 *  files are rooted at. A kind that needs more takes it from its own hooks. */
export interface WorkspaceCtx {
  chatId: string;
  driveId: string;
  /** The chat's working directory — the root every tab resolves under. */
  rootNodeId: string;
  /** Whether the machine serving this chat is reachable, as the chat's banner
   *  reads it. Unset while the page has no word on it. A tab that shows the
   *  folder as live reads this so it can never say "Live" under a banner
   *  saying the workspace is unreachable. */
  machineReady?: boolean;
  /** The socket a text file is edited live on. Unset on a surface that shows
   *  files only (and in a test that is not about live editing). */
  liveSocket?: LiveSocket;
  /** Ask the server to wake the chat's machine, for a tab that needs it
   *  running (a notebook). The server decides whether this reader may. */
  wake?: () => void;
}

export interface TabKind {
  kind: string;
  /** Never closable — the Files tab. */
  pinned?: boolean;
  /** Opening it twice activates the one already open. */
  singleton?: boolean;
  /** The strip's label. The stored name is the floor; the live item wins when
   *  there is one, so a file renamed on the machine reads as its new name. */
  label(tab: WorkspaceTab, item?: Item): string;
  Component: ComponentType<{ tab: WorkspaceTab; ctx: WorkspaceCtx }>;
}

/** What a tab of an unregistered kind says. It stays in the strip and stays in
 *  the stored document, so opening the chat on a newer client restores it. */
export const UNKNOWN_TAB_KIND_NOTICE = "This tab needs a newer version";

/** The ceiling the server enforces on a stored workspace. Spelled here too so
 *  the client can trim before it is refused. */
export const MAX_WORKSPACE_TABS = 32;

const KINDS = new Map<string, TabKind>();

/** Teach the workspace about a kind of tab. The same kind registered twice
 *  replaces the first, so a hot reload does not stack two renderers. */
export function registerTabKind(kind: TabKind): void {
  KINDS.set(kind.kind, kind);
}

/** The kind, or `undefined` when this build has never heard of it. */
export function tabKindFor(kind: string): TabKind | undefined {
  return KINDS.get(kind);
}

/** Every kind this build registered, in registration order. */
export function registeredTabKinds(): TabKind[] {
  return [...KINDS.values()];
}
