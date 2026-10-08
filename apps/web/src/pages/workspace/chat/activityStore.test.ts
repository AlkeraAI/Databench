// The owned copy of what the lists know about chats they are not showing the
// inside of. Rows read this store, never the mutable fold behind it, so the
// contract that matters is: each publish hands out a WHOLE new snapshot, an
// older snapshot a render already read stays exactly as it was, and a list that
// has stopped watching stops receiving.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { fold, fire, watchers } = vi.hoisted(() => {
  // What the data source's fold currently holds. Every getter returns a fresh
  // object, the way the real source does, so an old snapshot can never be
  // mutated out from under a component that read it.
  const fold = {
    activity: {} as Record<string, { awaiting: boolean; lastInteractionAt: string | null }>,
    subagents: [] as Array<{ id: string; parentSessionId: string }>,
    labels: {} as Record<string, string>,
  };
  const watchers = new Map<string, Set<() => void>>();
  const fire = (chatId: string): void => {
    for (const cb of watchers.get(chatId) ?? []) cb();
  };
  return { fold, fire, watchers };
});

vi.mock("./data", () => ({
  chatData: () => ({
    getChatActivity: () => ({ ...fold.activity }),
    getSubagentChats: () => [...fold.subagents],
    getSubagentLabels: () => ({ ...fold.labels }),
    subscribeChat: (chatId: string, cb: () => void) => {
      const set = watchers.get(chatId) ?? new Set<() => void>();
      set.add(cb);
      watchers.set(chatId, set);
      return () => set.delete(cb);
    },
  }),
}));

import type { ChatActivity } from "./data";

import { fillSendingGap, useChatActivity } from "./activityStore";

const BUSY: ChatActivity = { awaiting: true, lastInteractionAt: null };
const DONE: ChatActivity = { awaiting: false, lastInteractionAt: "2026-06-11T01:00:00Z" };

interface GapCase {
  case: string;
  activity: Record<string, ChatActivity>;
  expected: Record<string, ChatActivity>;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  watchers.clear();
  fold.activity = {};
  fold.subagents = [];
  fold.labels = {};
  useChatActivity.setState({ activity: {}, subagents: [], labels: {} });
});

describe("chat activity store", () => {
  it("publishes what the fold already holds the moment a list starts watching", () => {
    fold.activity = { c1: BUSY };
    fold.subagents = [{ id: "c2", parentSessionId: "c1" }];
    fold.labels = { c2: "Check the schema" };

    const stop = useChatActivity.getState().watch(["c1"]);

    expect(useChatActivity.getState().activity).toEqual({ c1: BUSY });
    expect(useChatActivity.getState().subagents).toEqual([{ id: "c2", parentSessionId: "c1" }]);
    expect(useChatActivity.getState().labels).toEqual({ c2: "Check the schema" });
    stop();
  });

  it("hands out a new snapshot on an event and leaves the old one as it was", () => {
    fold.activity = { c1: BUSY };
    const stop = useChatActivity.getState().watch(["c1"]);
    const first = useChatActivity.getState().activity;

    fold.activity = { c1: DONE };
    fire("c1");

    const second = useChatActivity.getState().activity;
    expect(second).not.toBe(first);
    expect(second).toEqual({ c1: DONE });
    expect(first).toEqual({ c1: BUSY });
    stop();
  });

  it("follows every chat in the list, not just the first", () => {
    fold.activity = {};
    const stop = useChatActivity.getState().watch(["c1", "c2"]);

    fold.activity = { c2: BUSY };
    fire("c2");

    expect(useChatActivity.getState().activity).toEqual({ c2: BUSY });
    stop();
  });

  it("re-publishes on the clock so a quiet list still ages its labels", () => {
    const stop = useChatActivity.getState().watch(["c1"]);

    fold.activity = { c1: DONE };
    vi.advanceTimersToNextTimer();

    expect(useChatActivity.getState().activity).toEqual({ c1: DONE });
    stop();
  });

  it("stops publishing once the list stops watching", () => {
    fold.activity = { c1: BUSY };
    const stop = useChatActivity.getState().watch(["c1"]);
    const held = useChatActivity.getState().activity;

    stop();
    fold.activity = { c1: DONE };
    fire("c1");
    vi.advanceTimersToNextTimer();

    expect(useChatActivity.getState().activity).toBe(held);
  });

  it("keeps publishing to a second list on the same chat after the first stops watching", () => {
    fold.activity = { c1: BUSY };
    const stopFirst = useChatActivity.getState().watch(["c1"]);
    const stopSecond = useChatActivity.getState().watch(["c1"]);

    stopFirst();
    fold.activity = { c1: DONE };
    fire("c1");

    expect(useChatActivity.getState().activity).toEqual({ c1: DONE });
    stopSecond();
  });
});

describe("filling the gap before the first event lands", () => {
  it.each<GapCase>([
    {
      case: "a chat this window just sent to, that the fold has never heard of, reads as working",
      activity: {},
      expected: { c9: { awaiting: true, lastInteractionAt: null } },
    },
    {
      case: "a chat the fold says already finished stays finished, whatever this window sent",
      activity: { c9: DONE },
      expected: { c9: DONE },
    },
    {
      case: "a chat the fold already calls working stays working",
      activity: { c9: BUSY },
      expected: { c9: BUSY },
    },
  ])("$case", ({ activity, expected }) => {
    expect(fillSendingGap(activity, new Set(["c9"]))).toEqual(expected);
  });

  it("changes nothing when this window has sent to no one", () => {
    const published = { c1: DONE };

    expect(fillSendingGap(published, new Set())).toEqual({ c1: DONE });
  });

  it("leaves the published snapshot untouched", () => {
    const published = { c1: DONE };

    const filled = fillSendingGap(published, new Set(["c1", "c9"]));

    expect(published).toEqual({ c1: DONE });
    expect(filled).not.toBe(published);
    expect(filled.c9).toEqual({ awaiting: true, lastInteractionAt: null });
  });
});
