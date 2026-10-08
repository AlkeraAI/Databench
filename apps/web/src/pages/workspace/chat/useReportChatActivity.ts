// Pushes the two signals the host's inactivity reaper can't infer — which chat
// the user is viewing, and each chat's working (`awaiting`) state — so the host
// can close chats that are neither viewed nor working. Reports on every
// navigation and on a slow interval (so a BACKGROUND chat finishing is reported
// even while the user sits on another surface / the chat list).

import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { chatData, chatHost } from "./data";

/**
 * The chat the user is currently viewing, derived from the route — or null when
 * not on a chat surface (the chat list, settings). Covers every chat-referencing
 * route (the live chat AND the editor overlays about a chat) so a chat is never
 * reaped while the user is looking at it or one of its detail views.
 */
export function viewedChatIdFromPath(pathname: string): string | null {
  const patterns: RegExp[] = [
    /^\/chat\/([^/]+)\/?$/,
    /^\/editor\/(?:chat|graph)\/([^/]+)/,
    /^\/editor\/(?:compaction|blobs?|activity|plan)\/([^/]+)/,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(pathname);
    if (match) return decodeURIComponent(match[1]);
  }
  return null;
}

/** How often to re-report (independent of navigation) so a background chat that
 *  finishes is seen as idle within the reaper's debounce window. */
export const REPORT_INTERVAL_MS = 20_000;

export function useReportChatActivity(): void {
  const { pathname } = useLocation();
  useEffect(() => {
    const report = (): void => {
      void chatHost().engine
        .request("chat.reportActivity", {
          viewedChatId: viewedChatIdFromPath(pathname),
          activity: chatData().getChatActivity(),
        })
        // A failed report is SAFE: the host stops refreshing its freshness clock,
        // so its reaper goes stale and reaps nothing rather than acting on a stale
        // view. Log for diagnosability; the next interval/navigation retries.
        .catch((err) => console.debug("[chat] chat.reportActivity failed:", err));
    };
    report(); // immediately on mount / navigation
    const timer = setInterval(report, REPORT_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [pathname]);
}
