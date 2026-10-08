// Adapt the open API responses into the overview's view-model, in one place so the
// page's components stay pure. An installed extension adds its own cards and panels
// through OVERVIEW_SOURCES; mergeOverview places them.

import type { IdentityDashboard, MyConnection } from "../../../api/dashboard";
import type { ChatSessionRead } from "../../../api/chats";
import type { OverviewSlice } from "../../../app/extensions/portal";
import { placeAfter } from "../../../app/extensions/portal";
import { UNTITLED_CHAT } from "../../../lib/chatTitle";
import type { ChatSummary, DashboardData, Stat } from "./model";

/** How many recent chats the overview lists before the panel stops growing. */
const RECENT_CHATS = 6;

/** The key of the open connections card, the anchor an extension's card names to follow it. */
export const CONNECTIONS_STAT = "connections";

/** An ISO timestamp → a coarse relative label ("2h ago"). */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const sec = Math.max(0, Math.round((now - then) / 1000));
  if (sec < 60) return "Just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day === 1) return "Yesterday";
  if (day < 7) return `${day}d ago`;
  const wk = Math.round(day / 7);
  return `${wk}w ago`;
}

/** The connections card — how many sources the caller can actually reach today.
 *
 *  No caption: "configured sources you can use" only restated the label the number already sits
 *  under, and a line of it under every count made the card look like it had something to report. */
function connectionsStat(connections: readonly MyConnection[]): Stat {
  return {
    key: CONNECTIONS_STAT,
    label: "Connections",
    value: connections.length.toLocaleString("en-US"),
    to: "/connections",
  };
}

/** The recent chats, newest first and capped — the panel is a way back in, not a chat list. */
function toChats(chats: readonly ChatSessionRead[], now: number): ChatSummary[] {
  return [...chats]
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, RECENT_CHATS)
    .map((c) => ({ id: c.id, title: c.title?.trim() || UNTITLED_CHAT, updated: relativeTime(c.updated_at, now) }));
}

export interface RawDashboardResponses {
  identity: IdentityDashboard;
  connections: MyConnection[];
  chats: ChatSessionRead[];
}

/** Compose the open view-model from the raw responses. `now` is injectable for
 *  deterministic relative-time labels under test. */
export function toDashboardData(raw: RawDashboardResponses, now: number = Date.now()): DashboardData {
  return {
    greetingName: raw.identity.user.first_name || raw.identity.user.display_name || "there",
    orgName: raw.identity.org.name,
    stats: [connectionsStat(raw.connections)],
    chats: toChats(raw.chats, now),
    panels: [],
  };
}

/** Place every installed source's cards and panels into the open view-model, in
 *  registration order. */
export function mergeOverview(base: DashboardData, slices: readonly OverviewSlice[]): DashboardData {
  const stats = placeAfter<Stat>(
    base.stats,
    slices.flatMap((slice) => slice.stats.map(({ stat, after }) => ({ item: stat, after, label: `overview card ${stat.key}` }))),
    (stat) => stat.key,
  );
  return { ...base, stats, panels: [...base.panels, ...slices.flatMap((slice) => slice.panels)] };
}
