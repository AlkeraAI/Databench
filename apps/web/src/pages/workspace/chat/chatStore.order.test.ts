// Two things the composer must not do on its own: send a fresh message past
// one the reader has not yet said to send, and carry the machine's last word
// about a turn into the next open of the chat.

import { afterEach, describe, expect, it, vi } from "vitest";
import { accountKey } from "@alkera/ui/storage";
import type { ConversationTurn } from "@alkera/chat-model";

const { ds, subscribers } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const ds = {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn(async (_chatId: string, _text: string) => ({
      id: "m1",
      role: "user",
      content: "x",
    })),
    cancelChat: vi.fn(async () => undefined),
    turnState: vi.fn((_id: string) => null as "working" | "idle" | null),
    subscribeChat: vi.fn((_id: string, cb: (event: { replay?: boolean }) => void) => {
      subscribers.push(cb);
      return () => {
        const i = subscribers.indexOf(cb);
        if (i >= 0) subscribers.splice(i, 1);
      };
    }),
  };
  return { ds, subscribers };
});

/** Who the queue is written down for, and where it lands for them. */
const ME = { userId: "usr_dana", orgId: "org_a" };
const queuedKey = (chatId: string): string => `${accountKey(ME.userId, ME.orgId, "chat.queued")}:${chatId}`;
vi.mock("./data", () => ({
  chatData: () => ds,
  chatHost: () => ({ account: () => ({ email: null, webAppUrl: null, ...ME }) }),
  isSessionNotOpen: () => false,
}));

import { useChatStore } from "./chatStore";

const flush = async (): Promise<void> => {
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
};

const userTurn = (id: string): ConversationTurn => ({
  id,
  author: "user",
  status: "done",
  parts: [{ id: `${id}-t`, kind: "text", text: "hi" }],
});
const assistantTurn = (id: string, completedAt?: string): ConversationTurn => ({
  id,
  author: "assistant",
  status: "done",
  completedAt,
  parts: [{ id: `${id}-t`, kind: "text", text: "…" }],
});
const DONE = "2026-09-21T00:00:00Z";

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  ds.getChatTurns.mockResolvedValue([]);
  ds.turnState.mockReturnValue(null);
  try {
    localStorage.clear();
  } catch {
    // nothing to clear
  }
});

async function live(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({}));
  await flush();
}

async function openWorking(): Promise<() => void> {
  const unsub = useChatStore.getState().open("c1");
  await flush();
  useChatStore.getState().send("c1", "go");
  await flush();
  ds.turnState.mockReturnValue("working");
  await live([userTurn("u1"), assistantTurn("a1")]);
  ds.sendUserMessage.mockClear();
  return unsub;
}

describe("a message typed behind a stopped one", () => {
  it("waits its turn instead of going past it", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "first");
    useChatStore.getState().cancel("c1");
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), assistantTurn("a1", DONE)]);
    expect(useChatStore.getState().byId.c1.queued.map((m) => m.stopped)).toEqual([true]);

    useChatStore.getState().send("c1", "second");
    await flush();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((m) => m.text)).toEqual(["first", "second"]);

    // The reader's word on the head sends it, and only it, first.
    const head = useChatStore.getState().byId.c1.queued[0].id;
    useChatStore.getState().sendQueued("c1", head);
    await flush();
    expect(ds.sendUserMessage.mock.calls.map((call) => call[1])).toEqual(["first"]);
    unsub();
  });

  it("waits behind a message restored from a previous page session", async () => {
    // Written by a page that is gone: it comes back `restored` and never sends
    // itself, and neither does anything typed after it go past it.
    localStorage.setItem(queuedKey("c1"), JSON.stringify([{ id: "q-old", text: "earlier" }]));
    const unsub = useChatStore.getState().open("c1");
    await flush();
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), assistantTurn("a1", DONE)]);
    expect(useChatStore.getState().byId.c1.queued.map((m) => m.restored)).toEqual([true]);

    useChatStore.getState().send("c1", "later");
    await flush();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((m) => m.text)).toEqual(["earlier", "later"]);
    unsub();
  });

  it("goes at once when nothing is held", async () => {
    const unsub = await openWorking();
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), assistantTurn("a1", DONE)]);
    useChatStore.getState().send("c1", "alone");
    await flush();
    expect(ds.sendUserMessage.mock.calls.map((call) => call[1])).toEqual(["alone"]);
    unsub();
  });
});

describe("the machine's last word", () => {
  it("does not outlive the open that heard it", async () => {
    const unsub = await openWorking();
    unsub();
    useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });

    // A fresh open of the same chat, from a source with no word yet: the
    // transcript is the only authority, and it says the turn is over.
    ds.turnState.mockReturnValue(null);
    ds.getChatTurns.mockResolvedValue([]);
    const reopen = useChatStore.getState().open("c1");
    await flush();
    useChatStore.getState().send("c1", "again");
    await flush();
    await live([userTurn("u1"), assistantTurn("a1", DONE)]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    reopen();
  });
});
