// The home list's order memory. The order moves only for news: a chat the list
// has never shown leads (freshest arrival first), a bump since the last pass
// leads next, and everything else holds the place the reader last saw it in.

import type { SubchatRowView } from "@alkera/ui";
import { describe, expect, it } from "vitest";

import type { Chat, ChatActivity } from "@/pages/workspace/chat/data/model";
import {
  buildRows,
  NO_ORDER,
  nextOrder,
  orderEntries,
  rowMarks,
  type HomeInputs,
  type OrderEntry,
  type OrderMemory,
} from "@/pages/workspace/chat/homeRows";

const entry = (id: string, freshAt: string, bumpAt?: string): OrderEntry =>
  bumpAt ? { id, freshAt, bumpAt } : { id, freshAt };

const chat = (over: Partial<Chat> = {}): Chat => ({
  id: "c1",
  title: "Hi",
  updatedAt: "2026-06-11T00:00:00Z",
  ...over,
});

const live = (over: Partial<ChatActivity> = {}): ChatActivity => ({
  awaiting: false,
  lastInteractionAt: null,
  ...over,
});

describe("rowMarks", () => {
  // The mark is a merge of three readings, in priority order: the live fold this
  // window observed, then the send in flight, then what the daemon reported on the
  // listing. `read` comes only from the wire's seen tail.
  it.each([
    ["never observed this session", chat(), undefined, false, { status: "idle" }],
    ["observed, no bump, no work", chat(), live(), false, { status: "idle" }],
    ["a watched turn boundary", chat(), live({ bumpAt: "2026-06-11T00:00:05Z" }), false, { status: "finished" }],
    ["the fold owes a response", chat(), live({ awaiting: true }), false, { status: "working" }],
    ["a send in flight, no fold", chat(), undefined, true, { status: "working" }],
    ["background jobs with the fold quiet", chat({ backgroundJobsRunning: 1 }), live(), false, { status: "working" }],
    ["the wire's running status, no fold", chat({ status: "running" }), undefined, false, { status: "working" }],
    // An ask outranks a stale bump on the fold, and the wire's spinner on the listing.
    ["a folded ask over a stale bump", chat(), live({ ask: "permission", awaiting: true, bumpAt: "2026-06-11T00:00:05Z" }), false, { status: "working", ask: "permission" }],
    ["the wire's pending ask over its spinner", chat({ status: "running", pendingAsk: "question" }), undefined, false, { status: "working", ask: "question" }],
    ["the wire's pending plan ask", chat({ status: "running", pendingAsk: "plan" }), undefined, false, { status: "working", ask: "plan" }],
    // `read` is truly absent, never false, unless the tails agree.
    ["matching wire tails", chat({ lastEventId: "e5", lastSeenEventId: "e5" }), undefined, false, { status: "finished", read: true }],
    ["diverged wire tails", chat({ lastEventId: "e5", lastSeenEventId: "e3" }), undefined, false, { status: "finished" }],
    ["never marked seen", chat({ lastEventId: "e5", lastSeenEventId: null }), undefined, false, { status: "finished" }],
    ["a turn boundary with no wire tail", chat(), live({ bumpAt: "2026-06-11T00:00:05Z" }), false, { status: "finished" }],
    // The live fold outranks a stale listing in both directions.
    ["a turn end over the wire's spinner and ask", chat({ status: "running", pendingAsk: "permission" }), live({ bumpAt: "2026-06-11T00:00:05Z" }), false, { status: "finished" }],
    ["an owed response over the wire's finish", chat({ lastEventId: "e5", lastSeenEventId: "e5" }), live({ awaiting: true }), false, { status: "working" }],
    ["a folded finish still reads the wire's seen tail", chat({ lastEventId: "e5", lastSeenEventId: "e5" }), live({ bumpAt: "2026-06-11T00:00:05Z" }), false, { status: "finished", read: true }],
  ] as const)("marks %s", (_why, c, activity, sending, expected) => {
    expect(rowMarks(c, activity, sending)).toEqual(expected);
  });
});

describe("buildRows", () => {
  const root = (id: string, updatedAt = "2026-06-11T00:00:00Z"): Chat => ({
    id,
    title: `Chat ${id}`,
    updatedAt,
  });

  const spawnedBy = (id: string, parent: string, updatedAt = "2026-06-11T00:00:00Z"): Chat => ({
    id,
    title: `Chat ${id}`,
    updatedAt,
    parentSessionId: parent,
  });

  const inputs = (over: Partial<HomeInputs>): HomeInputs => ({
    chats: [],
    subagents: [],
    activity: {},
    sending: new Set(),
    labels: {},
    ...over,
  });

  /** Every id in the tree, depth-first — so a repeat shows up as a duplicate. */
  const ids = (rows: readonly SubchatRowView[]): string[] =>
    rows.flatMap((row) => [row.id, ...ids(row.children ?? [])]);

  it("hangs a grandchild under its parent subchat, never the root", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [spawnedBy("a", "r"), spawnedBy("g", "a")],
      }),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].children?.map((child) => child.id)).toEqual(["a"]);
    expect(rows[0].children?.[0].children?.map((child) => child.id)).toEqual(["g"]);
  });

  it("orders siblings freshest-first at every level", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [
          spawnedBy("old", "r", "2026-06-11T00:00:01Z"),
          spawnedBy("new", "r", "2026-06-11T00:00:03Z"),
          spawnedBy("mid", "r", "2026-06-11T00:00:02Z"),
          spawnedBy("g-old", "new", "2026-06-11T00:00:01Z"),
          spawnedBy("g-new", "new", "2026-06-11T00:00:02Z"),
        ],
      }),
    );
    expect(rows[0].children?.map((child) => child.id)).toEqual(["new", "mid", "old"]);
    expect(rows[0].children?.[0].children?.map((child) => child.id)).toEqual(["g-new", "g-old"]);
  });

  it("a streamed event outranks the stored manifest time in the order", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [
          spawnedBy("a", "r", "2026-06-11T00:00:05Z"),
          spawnedBy("b", "r", "2026-06-11T00:00:01Z"),
        ],
        activity: { b: live({ lastEventAt: "2026-06-11T00:00:09Z" }) },
      }),
    );
    expect(rows[0].children?.map((child) => child.id)).toEqual(["b", "a"]);
  });

  it("leaves children unset on a chat that spawned nothing", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r"), root("lone")],
        subagents: [spawnedBy("a", "r")],
      }),
    );
    expect(rows[0].children?.[0].children).toBeUndefined();
    expect(rows[1].children).toBeUndefined();
  });

  it("renders a self-parented chat once and never hangs the build", () => {
    const rows = buildRows(
      inputs({
        chats: [root("s")],
        subagents: [spawnedBy("s", "s")],
      }),
    );
    expect(ids(rows)).toEqual(["s"]);
    expect(rows[0].children).toBeUndefined();
  });

  it("a root claiming its own descendant as parent ends that walk", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [spawnedBy("a", "r"), spawnedBy("b", "a"), spawnedBy("r", "b")],
      }),
    );
    expect(ids(rows)).toEqual(["r", "a", "b"]);
    expect(rows[0].children?.[0].children?.[0].children).toBeUndefined();
  });

  it("renders a chat listed as both root and subagent once", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r1"), root("r2")],
        subagents: [spawnedBy("r2", "r1")],
      }),
    );
    expect(rows.map((chat) => chat.id)).toEqual(["r1", "r2"]);
    expect(rows[0].children).toBeUndefined();
  });

  it("renders a subagent listed twice once, under its parent", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [spawnedBy("a", "r"), spawnedBy("a", "r")],
      }),
    );
    expect(ids(rows)).toEqual(["r", "a"]);
  });

  it("drops a subagent whose parent is not listed", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [spawnedBy("x", "ghost")],
      }),
    );
    expect(ids(rows)).toEqual(["r"]);
  });

  it("names a child by the parent's brief, the root by its manifest", () => {
    const rows = buildRows(
      inputs({
        chats: [root("r")],
        subagents: [
          spawnedBy("a", "r", "2026-06-11T00:00:02Z"),
          spawnedBy("b", "r", "2026-06-11T00:00:01Z"),
        ],
        labels: { a: "Research the docs", r: "Never used" },
      }),
    );
    expect(rows[0].title).toBe("Chat r");
    expect(rows[0].children?.map((child) => child.title)).toEqual(["Research the docs", "Chat b"]);
  });

  it("carries a freshness label only when a timestamp exists", () => {
    const rows = buildRows(
      inputs({
        chats: [root("blank", ""), root("dated", "2026-06-11T00:00:00Z")],
      }),
    );
    expect(rows[0].freshness).toBeUndefined();
    expect(rows[1].freshness).toEqual(expect.any(String));
  });
});

describe("nextOrder", () => {
  it("leads with arrivals freshest-first on the first sighting", () => {
    const next = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:01Z"),
      entry("b", "2026-06-11T00:00:03Z"),
      entry("c", "2026-06-11T00:00:02Z"),
    ]);
    expect(next.order).toEqual(["b", "c", "a"]);
  });

  it("holds the order under a re-run with the same entries (idempotent)", () => {
    const entries = [
      entry("a", "2026-06-11T00:00:01Z", "2026-06-11T00:00:01Z"),
      entry("b", "2026-06-11T00:00:03Z"),
      entry("c", "2026-06-11T00:00:02Z"),
    ];
    const first = nextOrder(NO_ORDER, entries);
    const second = nextOrder(first, entries);
    const third = nextOrder(second, entries);
    expect(second.order).toEqual(first.order);
    expect(third.order).toEqual(first.order);
  });

  it("moves a bumped chat to the top and holds the rest in place", () => {
    const seen = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T00:00:02Z"),
      entry("c", "2026-06-11T00:00:01Z"),
    ]);
    expect(seen.order).toEqual(["a", "b", "c"]);
    const bumped = nextOrder(seen, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T00:09:00Z", "2026-06-11T00:09:00Z"),
      entry("c", "2026-06-11T00:00:01Z"),
    ]);
    expect(bumped.order).toEqual(["b", "a", "c"]);
  });

  it("does not move a chat whose freshness advanced without a bump", () => {
    const seen = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T00:00:02Z"),
    ]);
    // b streams tokens: its freshAt races ahead, but the reader's list holds.
    const streaming = nextOrder(seen, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T09:00:00Z"),
    ]);
    expect(streaming.order).toEqual(["a", "b"]);
  });

  it("an already-accounted bump is not news, a changed one is", () => {
    const bumpedOnce = nextOrder(
      nextOrder(NO_ORDER, [entry("a", "T1"), entry("b", "T0")]),
      [entry("a", "T1"), entry("b", "T2", "T2")],
    );
    expect(bumpedOnce.order).toEqual(["b", "a"]);
    const settled = nextOrder(bumpedOnce, [entry("a", "T3", "T3"), entry("b", "T2", "T2")]);
    expect(settled.order).toEqual(["a", "b"]);
    const held = nextOrder(settled, [entry("a", "T3", "T3"), entry("b", "T2", "T2")]);
    expect(held.order).toEqual(["a", "b"]);
  });

  it("leads with an arrival ahead of a bump in the same pass", () => {
    const seen = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T00:00:02Z"),
    ]);
    const next = nextOrder(seen, [
      entry("a", "2026-06-11T00:05:00Z", "2026-06-11T00:05:00Z"),
      entry("b", "2026-06-11T00:00:02Z"),
      // The new chat is older than a's bump, yet its first sighting still leads.
      entry("new", "2026-06-11T00:04:00Z"),
    ]);
    expect(next.order).toEqual(["new", "a", "b"]);
  });

  it("orders several bumps in one pass newest bump first", () => {
    const seen = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:04Z"),
      entry("b", "2026-06-11T00:00:03Z"),
      entry("c", "2026-06-11T00:00:02Z"),
      entry("d", "2026-06-11T00:00:01Z"),
    ]);
    const next = nextOrder(seen, [
      entry("a", "2026-06-11T00:00:04Z"),
      entry("b", "2026-06-11T00:05:00Z", "2026-06-11T00:05:00Z"),
      entry("c", "2026-06-11T00:00:02Z"),
      entry("d", "2026-06-11T00:07:00Z", "2026-06-11T00:07:00Z"),
    ]);
    expect(next.order).toEqual(["d", "b", "a", "c"]);
  });

  it("drops a chat that is gone from the entries", () => {
    const seen = nextOrder(NO_ORDER, [
      entry("a", "2026-06-11T00:00:03Z"),
      entry("b", "2026-06-11T00:00:02Z"),
      entry("c", "2026-06-11T00:00:01Z"),
    ]);
    const next = nextOrder(seen, [entry("a", "2026-06-11T00:00:03Z"), entry("c", "2026-06-11T00:00:01Z")]);
    expect(next.order).toEqual(["a", "c"]);
  });

  it("records the bump each id was placed at, for the next pass", () => {
    const memory: OrderMemory = nextOrder(NO_ORDER, [entry("a", "T1", "T1"), entry("b", "T0")]);
    expect(memory.bumps.get("a")).toBe("T1");
    expect(memory.bumps.get("b")).toBeUndefined();
    expect(memory.bumps.has("b")).toBe(true);
  });
});

describe("orderEntries", () => {
  it("carries a bump only for chats with live bumped activity", () => {
    const chats: Chat[] = [
      { id: "a", title: "A", updatedAt: "2026-06-11T00:00:01Z" },
      { id: "b", title: "B", updatedAt: "2026-06-11T00:00:02Z" },
    ];
    const activity: Record<string, ChatActivity | undefined> = {
      a: { awaiting: false, lastInteractionAt: null, bumpAt: "2026-06-11T00:00:05Z" },
    };
    expect(orderEntries(chats, activity)).toEqual([
      { id: "a", freshAt: "2026-06-11T00:00:01Z", bumpAt: "2026-06-11T00:00:05Z" },
      { id: "b", freshAt: "2026-06-11T00:00:02Z" },
    ]);
  });
});
