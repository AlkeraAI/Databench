// The pane beside the conversation: editor groups, each a strip of tabs over
// one panel, split the way the reader split them.
//
// It is deliberately thin. The document that says what is open lives on the
// server and is kept honest by `useWorkspaceState`; what each tab DRAWS comes
// from the kind registry, which is why this file imports the two kinds rather
// than switching on them — a kind added later is a module the pane loads, not a
// branch somebody has to remember to add here.
//
// Only the tab in front of each group is mounted. A workspace can hold dozens
// of tabs, and a background tab that kept a frame, a blob URL and a live content
// grant alive for a file nobody is reading would cost the reader memory and the
// drive requests for nothing.
//
// Which is why the pane, and not the tab, is what hears the drive: a background
// tab has no hooks running to notice its own file move, so the strip would go
// on naming a file the machine rewrote ten minutes ago. The pane watches the
// node of every tab in the strip and marks the ones the reader is not in.

import { useEffect, useMemo, useRef, type KeyboardEvent, type ReactElement } from "react";
import { useQueries } from "@tanstack/react-query";

import { ToastViewport, useToasts } from "@alkera/ui";

import { api, request } from "@/api/client";
import type { LiveSocket } from "@/api/realtime/crdt/channel";
import { useFrames } from "@/api/events/frameBus";
import { useItem, type Item } from "@/api/files";
import { keys } from "@/api/keys";
import { detectPlatform } from "@/lib/platform";

import { EditorGroups } from "./EditorGroups";
import { editorCommandFor } from "./editorShortcuts";
import { toggledView } from "./FileTab";
import { groupOrder } from "./editorLayout";
import { tabKindFor, type WorkspaceCtx, type WorkspaceTab } from "./tabKinds";
import { useWorkspaceState } from "./useWorkspaceState";
import { FILES_TAB_ID, groupById, useWorkspaceStore, workspaceOf } from "./workspaceStore";

// Loaded for their registrations: the workspace learns what a `files` tab and a
// `file` tab are by the modules that draw them being here.
import "./FilesTab";
import "./FileTab";

import "./workspace.css";


/** The reasons a node frame carries when the machine holding the folder changed
 *  the file's bytes: a save landed, a batch of saves landed, a conflicted copy
 *  was made, or the reader's own inbound write was superseded. A frame with no
 *  reason is the node moving, not its content. */
const CONTENT_CHANGE_REASONS: ReadonlySet<string> = new Set([
  "live_saved",
  "live_batch",
  "conflict",
  "inbound_superseded",
]);

export interface ChatSidePaneProps {
  chatId: string;
  driveId: string;
  /** The chat's working directory — the root every tab resolves under. */
  rootNodeId: string;
  /** Whether the machine serving the chat is reachable, from the same read
   *  the banner over the composer follows. Unset while that read is in flight. */
  machineReady?: boolean;
  /** The socket text files are edited live on; unset, files only preview. */
  liveSocket?: LiveSocket;
  /** Wakes the chat's machine for a tab that needs it running. */
  onWake?: () => void;
}

export function ChatSidePane({
  chatId,
  driveId,
  rootNodeId,
  machineReady,
  liveSocket,
  onWake,
}: ChatSidePaneProps): ReactElement {
  // The root's own path is what tells a tab whose file has been MOVED OUT of
  // the chat from one that is merely unreadable. The Files tab reads the same
  // node under the same key, so this costs no second request.
  const root = useItem(driveId, rootNodeId);
  const { entry, ready } = useWorkspaceState(chatId, driveId, root.data?.pathBytes);
  const closeTab = useWorkspaceStore((state) => state.closeTab);
  const splitTab = useWorkspaceStore((state) => state.splitTab);
  const setTabView = useWorkspaceStore((state) => state.setTabView);
  const focusGroup = useWorkspaceStore((state) => state.focusGroup);
  const clearNotice = useWorkspaceStore((state) => state.clearNotice);
  const markUpdated = useWorkspaceStore((state) => state.markUpdated);

  // The same reads `useWorkspaceState` already holds, under the same keys, so
  // naming a tab after its live file costs no request — only the name.
  const nodeIds = useMemo(
    () => [
      ...new Set(
        entry.tabs
          .filter((tab): tab is WorkspaceTab & { node_id: string } => typeof tab.node_id === "string")
          .map((tab) => tab.node_id),
      ),
    ],
    [entry.tabs],
  );
  const items = useQueries({
    queries: nodeIds.map((nodeId) => ({
      queryKey: keys.files.item(nodeId),
      queryFn: () =>
        request(
          api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}", {
            params: { path: { drive_id: driveId, item_id: nodeId } },
          }),
        ) as Promise<Item>,
    })),
    combine: (results) => {
      const byId: Record<string, Item> = {};
      results.forEach((result, index) => {
        const nodeId = nodeIds[index];
        if (nodeId && result.data) byId[nodeId] = result.data;
      });
      return byId;
    },
  });

  // Every open tab's node, watched from here. Read through a ref inside the
  // predicate so opening and closing tabs never drops the frames that land in
  // the gap a re-subscription would leave. A node frame is the drive saying
  // that node moved; a lease frame is about a folder's in-flight plane, which
  // is the mounted tab's business and not the strip's.
  const watched = useRef<readonly string[]>(nodeIds);
  watched.current = nodeIds;
  useFrames(
    // Only a frame that says the machine changed the file's CONTENT marks the
    // tab. A node frame with no reason is a share, a rename, a move or a trash
    // — the node changed, the bytes did not — and a dot for it reads as an
    // edit that never happened. A mounted tab also watches its own content tag.
    (frame) =>
      frame.type === "file_node.changed" &&
      CONTENT_CHANGE_REASONS.has(frame.reason ?? "") &&
      watched.current.includes(frame.entity_id),
    // The store refuses to mark the tab in front: a change the reader is
    // watching happen needs no announcement.
    (frame) => markUpdated(chatId, frame.entity_id),
  );

  // The store closed tabs to get the document under the server's ceiling. That
  // is something done TO the reader's workspace while they were not looking, so
  // it is said out loud once and then forgotten.
  const { toasts, push, dismiss } = useToasts();
  const notice = entry.notice;
  useEffect(() => {
    if (!notice) return;
    push({ id: "workspace-trim", tone: "info", message: notice });
    clearNotice(chatId);
  }, [chatId, clearNotice, notice, push]);

  const ctx: WorkspaceCtx = useMemo(
    () => ({ chatId, driveId, rootNodeId, machineReady, liveSocket, ...(onWake ? { wake: onWake } : {}) }),
    [chatId, driveId, rootNodeId, machineReady, liveSocket, onWake],
  );
  const itemOf = (tab: WorkspaceTab): Item | undefined => (tab.node_id ? items[tab.node_id] : undefined);

  // The editor's keyboard, anywhere inside the pane. Taken in the capture phase
  // so an editor inside a tab never sees a chord that belongs to the workspace.
  const platform = useMemo(() => detectPlatform(), []);
  const onKeyDownCapture = (event: KeyboardEvent<HTMLElement>): void => {
    const command = editorCommandFor(event, platform);
    if (command === null) return;
    const current = workspaceOf(chatId);
    if (!current?.hydrated) return;
    const front = current.tabs.find((tab) => tab.id === current.activeTabId);
    event.preventDefault();
    event.stopPropagation();
    switch (command.kind) {
      case "split":
        splitTab(chatId, "right");
        return;
      case "close":
        if (front && front.id !== FILES_TAB_ID && tabKindFor(front.kind)?.pinned !== true) closeTab(chatId, front.id);
        return;
      case "toggle-preview": {
        if (!front || front.kind !== "file") return;
        const next = toggledView(itemOf(front), front.view);
        if (next !== null) setTabView(chatId, front.id, next);
        return;
      }
      case "focus-group": {
        const id = groupOrder(current.layout)[command.index];
        if (id === undefined) return;
        focusGroup(chatId, id);
        // The keyboard goes where the reader went: onto the tab in front of
        // that group, from where its strip and its panel are one key away.
        const tabId = groupById(current, id)?.activeTabId;
        if (tabId) requestAnimationFrame(() => document.getElementById(`tab-${tabId}`)?.focus());
        return;
      }
    }
  };

  // The strip waits for the chat's own document. Everything above this line has
  // run — the pane is mounted, the read is out, the frames are subscribed — but
  // what the store holds until the document lands is the workspace every chat
  // starts from, and a reader coming back to their tabs must not first be shown
  // a workspace that has none of them.
  if (!ready) return <section className="alk-ws" aria-label="Chat files" />;

  return (
    <section className="alk-ws" aria-label="Chat files" onKeyDownCapture={onKeyDownCapture}>
      <EditorGroups chatId={chatId} entry={entry} ctx={ctx} itemOf={itemOf} />
      <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-right" />
    </section>
  );
}
