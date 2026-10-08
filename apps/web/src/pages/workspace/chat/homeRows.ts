// What the home list says, and the order it holds it in.
//
// The order moves only on a BUMP -- a turn ending, or an ask arriving -- so a
// streamed token can age a row's label without sliding the row out from under
// the reader's pointer. Everything here is pure; the surface owns the fetching.

import type { ChatAskKind, ChatRowStatus, ChatRowView, SubchatRowView } from "@alkera/ui";
import { formatChatUpdatedAt } from "@alkera/ui";
import type { Chat, ChatActivity } from "./data";

export interface HomeInputs {
  /** Root chats, as the daemon listed them. */
  chats: readonly Chat[];
  /** Subagent chats, each naming the parent that spawned it. */
  subagents: readonly Chat[];
  activity: Record<string, ChatActivity | undefined>;
  /** Chats this webview has just sent to, before their first event lands. A
   *  chat with folded activity ignores this: the fold is the authority. */
  sending: ReadonlySet<string>;
  /** What each parent calls its children. A spawned chat's manifest carries no
   *  title, so the parent's brief is the better name whenever it is known. */
  labels: Record<string, string>;
}

/** The freshest thing known about a chat. A streamed event outranks the
 *  manifest, which only moves when the daemon writes it. ISO-8601 compares
 *  lexicographically. */
export function freshAt(chat: Chat, live: ChatActivity | undefined): string {
  const streamed = live?.lastEventAt ?? live?.lastInteractionAt ?? "";
  const stored = chat.updatedAt ?? "";
  return streamed > stored ? streamed : stored;
}

/** The row's mark. An ask only exists on a live turn, so it rides with
 *  "working" and the row shows it in the mark's place. The live fold is the
 *  authority wherever it has state (it is fresher than any listing); the wire
 *  fills the cold gap — a reloaded window still shows each chat's spinner,
 *  pending ask, and finished dot from what the daemon reported. The dot's
 *  read/unread step always comes from the wire's seen tail, the only record
 *  of what this user has rendered. */
export function rowMarks(
  chat: Chat,
  live: ChatActivity | undefined,
  sending: boolean,
): { status: ChatRowStatus; ask?: ChatAskKind; read?: boolean } {
  if (live?.ask) return { status: "working", ask: live.ask };
  const awaiting = live ? live.awaiting : sending;
  if (awaiting || (chat.backgroundJobsRunning ?? 0) > 0) return { status: "working" };
  if (!live) {
    if (chat.pendingAsk) return { status: "working", ask: chat.pendingAsk };
    if (chat.status === "running") return { status: "working" };
  }
  if (live?.bumpAt || chat.lastEventId) {
    // `read` only when the wire proves it: the seen tail matches the event
    // tail. Absent means unread or unknown, and the dot stays vibrant.
    const read = !!chat.lastEventId && chat.lastSeenEventId === chat.lastEventId;
    return read ? { status: "finished", read } : { status: "finished" };
  }
  return { status: "idle" };
}

function rowOf(chat: Chat, inputs: HomeInputs, title: string): SubchatRowView {
  const live = inputs.activity[chat.id];
  const row: SubchatRowView = {
    id: chat.id,
    title,
    ...rowMarks(chat, live, inputs.sending.has(chat.id)),
  };
  const freshness = formatChatUpdatedAt(freshAt(chat, live));
  if (freshness) row.freshness = freshness;
  return row;
}

/** One row per root chat, each carrying the subagent chats it spawned, however
 *  deep the spawning ran. Freshest sibling first at every level, so a chat's
 *  newest delegation leads. A corrupt manifest can point parent ids in a
 *  circle, so a chat renders once: a repeat id ends that walk, never the
 *  build. */
export function buildRows(inputs: HomeInputs): ChatRowView[] {
  const spawned = new Map<string, Chat[]>();
  const ordered = [...inputs.subagents].sort(
    (a, b) => freshAt(b, inputs.activity[b.id]).localeCompare(freshAt(a, inputs.activity[a.id])),
  );
  for (const chat of ordered) {
    const parent = chat.parentSessionId;
    if (!parent) continue;
    const siblings = spawned.get(parent) ?? [];
    siblings.push(chat);
    spawned.set(parent, siblings);
  }
  const childRows = (parentId: string, seen: Set<string>): SubchatRowView[] => {
    const rows: SubchatRowView[] = [];
    for (const chat of spawned.get(parentId) ?? []) {
      if (seen.has(chat.id)) continue;
      seen.add(chat.id);
      const row = rowOf(chat, inputs, inputs.labels[chat.id] ?? chat.title);
      const kids = childRows(chat.id, seen);
      if (kids.length > 0) row.children = kids;
      rows.push(row);
    }
    return rows;
  };
  const seen = new Set(inputs.chats.map((chat) => chat.id));
  return inputs.chats.map((chat) => {
    const row: ChatRowView = rowOf(chat, inputs, chat.title);
    const kids = childRows(chat.id, seen);
    if (kids.length > 0) row.children = kids;
    return row;
  });
}

export interface OrderEntry {
  id: string;
  /** When this chat last earned the top of the list. */
  bumpAt?: string;
  /** Ranks a chat the list has never shown before. */
  freshAt?: string;
}

export interface OrderMemory {
  order: readonly string[];
  /** The bump each id was last placed at, so a new one can be told from a
   *  repeat of the one already accounted for. */
  bumps: ReadonlyMap<string, string | undefined>;
}

export const NO_ORDER: OrderMemory = { order: [], bumps: new Map() };

export function orderEntries(
  chats: readonly Chat[],
  activity: Record<string, ChatActivity | undefined>,
): OrderEntry[] {
  return chats.map((chat) => {
    const live = activity[chat.id];
    const entry: OrderEntry = { id: chat.id, freshAt: freshAt(chat, live) };
    if (live?.bumpAt) entry.bumpAt = live.bumpAt;
    return entry;
  });
}

function newestFirst<T>(items: T[], key: (item: T) => string | undefined): T[] {
  return [...items].sort((a, b) => (key(b) ?? "").localeCompare(key(a) ?? ""));
}

/** The next order, given the one the reader is looking at. A chat leads when it
 *  has just appeared (it was created moments ago, or this is the first sighting
 *  of the whole list) or when it has bumped since the last pass. Everything else
 *  holds the place it had, and a chat that is gone drops out. */
export function nextOrder(prev: OrderMemory, entries: readonly OrderEntry[]): OrderMemory {
  const bumps = new Map<string, string | undefined>();
  const arrived: OrderEntry[] = [];
  const bumped: OrderEntry[] = [];
  for (const entry of entries) {
    bumps.set(entry.id, entry.bumpAt);
    if (!prev.bumps.has(entry.id)) arrived.push(entry);
    else if (entry.bumpAt && entry.bumpAt !== prev.bumps.get(entry.id)) bumped.push(entry);
  }
  const lead = [
    ...newestFirst(arrived, (entry) => entry.freshAt),
    ...newestFirst(bumped, (entry) => entry.bumpAt),
  ].map((entry) => entry.id);
  const leading = new Set(lead);
  const held = prev.order.filter((id) => bumps.has(id) && !leading.has(id));
  return { order: [...lead, ...held], bumps };
}

/** The rows in the held order. A row the order has not placed yet trails, so a
 *  list is never short a chat whatever the order says. */
export function applyOrder(rows: readonly ChatRowView[], order: readonly string[]): ChatRowView[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const placed: ChatRowView[] = [];
  for (const id of order) {
    const row = byId.get(id);
    if (row) {
      placed.push(row);
      byId.delete(id);
    }
  }
  return [...placed, ...byId.values()];
}
