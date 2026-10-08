// Messages typed while a turn is running are QUEUED, not swallowed.
//
// The composer is busy for the length of a turn, so the send it refuses has to
// go somewhere the reader can see: the message is held against the chat and
// goes out on the turn's terminal event, through the same send path a typed
// message takes. It goes exactly once, it can be taken back before it goes,
// and it survives the tab being reloaded.

import { afterEach, describe, expect, it, vi } from "vitest";
import { accountKey } from "@alkera/ui/storage";
import type { ConversationTurn } from "@alkera/chat-model";

const { ds, subscribers } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const ds = {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn(async (_chatId: string, _text: string, _opts?: unknown) => ({
      id: "m1",
      role: "user",
      content: "x",
    })),
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
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
};

function userTurn(id: string): ConversationTurn {
  return {
    id,
    author: "user",
    status: "done",
    parts: [{ id: `${id}-t`, kind: "text", text: "hi" }],
  };
}

function assistantTurn(id: string, opts: { completedAt?: string } = {}): ConversationTurn {
  return {
    id,
    author: "assistant",
    status: "done",
    completedAt: opts.completedAt,
    parts: [{ id: `${id}-t`, kind: "text", text: "…" }],
  };
}

const DONE = "2026-09-05T00:00:00Z";
const RUNNING = [userTurn("u1"), assistantTurn("a1")];
const FINISHED = [userTurn("u1"), assistantTurn("a1", { completedAt: DONE })];

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  ds.getChatTurns.mockResolvedValue([]);
  ds.turnState.mockReturnValue(null);
  try {
    localStorage.clear();
  } catch {
    // A browser that refuses the store leaves nothing to clear.
  }
});

async function live(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({}));
  await flush();
}

/** Open a chat with a turn already in flight from this tab. */
async function openWorking(): Promise<() => void> {
  const unsub = useChatStore.getState().open("c1");
  await flush();
  useChatStore.getState().send("c1", "go");
  await flush();
  ds.turnState.mockReturnValue("working");
  await live(RUNNING);
  ds.sendUserMessage.mockClear();
  return unsub;
}

describe("queued messages", () => {
  it("holds a message typed during a turn instead of sending it", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "and then deploy it",
    ]);
    unsub();
  });

  it("sends the queued message exactly once when the turn ends", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");

    // The terminal event: the transcript settles and the box goes idle.
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);

    // Every later idle reconcile must not send it again.
    await live(FINISHED);
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    unsub();
  });

  it("sends nothing for a message taken back before the turn ends", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "never mind");
    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().removeQueued("c1", held.id);
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);

    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    unsub();
  });

  it("sends the edited words, not the ones first typed", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "deploy it");
    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().editQueued("c1", held.id, "deploy it to staging");

    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("deploy it to staging");
    unsub();
  });

  it("sends one at a time, in the order they were typed", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "first");
    useChatStore.getState().queueMessage("c1", "second");

    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("first");
    // The second waits behind the turn the first one started.
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual(["second"]);
    unsub();
  });

  it("keeps the queue when the reader leaves the chat and comes back", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    unsub();

    const again = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "and then deploy it",
    ]);
    again();
  });

  it("keeps the queue across a reload of the tab", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    unsub();

    // A reload: nothing in memory survives, only what was written down.
    useChatStore.setState({ byId: {}, pending: {} });
    const again = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "and then deploy it",
    ]);
    again();
  });

  it("forgets a sent message, so a reload cannot send it a second time", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    unsub();

    useChatStore.setState({ byId: {}, pending: {} });
    ds.sendUserMessage.mockClear();
    const again = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    again();
  });
});

/** Put a message in the browser's copy the way a previous page session would
 *  have left it, with nothing at all in memory. */
function leftBehind(chatId: string, text: string): void {
  localStorage.setItem(
    queuedKey(chatId),
    JSON.stringify([{ id: "q-before", text }]),
  );
}

describe("a queue restored from a previous page session", () => {
  it("sends nothing when the chat it opens onto is idle", async () => {
    // The turn it was waiting behind ended while the tab was gone. Opening a
    // chat must never put words in it by itself: the reader is not here to see
    // it happen, the intent may be hours old, and it spends their credit.
    leftBehind("c1", "and then deploy it");
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");

    const unsub = useChatStore.getState().open("c1");
    await flush();
    await live(FINISHED);

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "and then deploy it",
    ]);
    unsub();
  });

  it("is marked as not sent, so the composer can say so", async () => {
    leftBehind("c1", "and then deploy it");
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");
    const unsub = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.queued[0].restored).toBe(true);
    unsub();
  });

  it("sends exactly once when the reader asks for it", async () => {
    leftBehind("c1", "and then deploy it");
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");
    const unsub = useChatStore.getState().open("c1");
    await flush();

    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().sendQueued("c1", held.id);
    await flush();

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);

    // And no later reconcile sends it again.
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    unsub();
  });

  it("sends nothing for one the reader takes back instead", async () => {
    leftBehind("c1", "never mind");
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");
    const unsub = useChatStore.getState().open("c1");
    await flush();

    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().removeQueued("c1", held.id);
    await live(FINISHED);

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);
    unsub();
  });

  it("holds a message typed THIS session behind the restored one", async () => {
    // The order the reader typed them in is the order the agent must see, so a
    // fresh message cannot jump the one still waiting on a click.
    leftBehind("c1", "the old one");
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");
    const unsub = useChatStore.getState().open("c1");
    await flush();

    useChatStore.getState().queueMessage("c1", "the new one");
    await live(FINISHED);
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "the old one",
      "the new one",
    ]);
    unsub();
  });

  it("asked for mid-turn, goes when the turn ends — once", async () => {
    leftBehind("c1", "and then deploy it");
    ds.getChatTurns.mockResolvedValue(RUNNING);
    ds.turnState.mockReturnValue("working");
    const unsub = useChatStore.getState().open("c1");
    await flush();
    await live(RUNNING);

    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().sendQueued("c1", held.id);
    await flush();
    // Nothing goes into a running turn.
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued[0].restored).toBe(false);

    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    await live(FINISHED);
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    unsub();
  });
});
