// Helper that opens an Alkera surface in a new editor webview tab when
// running inside VS Code, or navigates the in-page router in the browser.
//
// `title` is the human-readable name (e.g. "Build monthly_active_customers
// mart"). The extension uses it for the panel tab; without it the tab
// shows the slug (`graph-jaffle-mart`).

import type { NavigateFunction } from "react-router-dom";
import type { Chat, SendOptions } from "./data";
import { chatHost } from "./data";

export interface SurfaceOpener {
  navigate: NavigateFunction;
}

// First message handed from the sidecar to ChatSurface when starting a NEW chat.
// ChatSurface renders it optimistically (user bubble + working indicator) while
// createChat opens the harness in the background — so a new chat never blocks
// the UI on the ~2s harness spawn.
export interface ChatHandoff {
  pendingMessage: string;
  pendingOptions?: SendOptions | null;
  /** A chat already opened for this message's files (the home composer's
   *  attachments are staged into it before Send). The surface sends the
   *  message INTO it instead of opening a second chat. */
  into?: Chat | null;
}

// Start a brand-new chat: navigate to ChatSurface immediately with the first
// message in nav state, rather than awaiting createChat before navigating.
export function startNewChat(
  message: string,
  options: SendOptions | undefined,
  { navigate }: SurfaceOpener,
  into?: Chat,
): void {
  navigate("/chat", {
    state: {
      pendingMessage: message,
      pendingOptions: options ?? null,
      into: into ?? null,
    } satisfies ChatHandoff,
  });
}

export function openGraph(
  graphId: string,
  title: string,
  { navigate }: SurfaceOpener,
): void {
  if (chatHost().kind === "vscode") {
    void chatHost().runCommand({
      command: "alkera.openGraphEditor",
      args: { graphId, title },
    });
    return;
  }
  navigate(`/editor/graph/${graphId}`);
}

export function openChatInEditor(
  chatId: string,
  _title: string,
  { navigate }: SurfaceOpener,
): void {
  // The activity-bar webview navigates internally to /chat/:id: the sidecar IS
  // the chat surface, so a chat never opens as a separate editor panel. Graphs
  // open as editor panels via openGraph().
  navigate(`/chat/${chatId}`);
}

export function focusChatSidebar({ navigate }: SurfaceOpener): void {
  if (chatHost().kind === "vscode") {
    void chatHost().runCommand({ command: "alkera.toggleChatSidebar" });
    return;
  }
  navigate("/chat");
}
