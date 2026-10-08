// A Stop cancels what the composer is holding — it never sends it.
//
// A message typed mid-turn is held in THIS browser: the server has never seen
// it. Pressing Stop is the reader saying they want the turn to end, and the
// end of the turn is exactly what the hold was waiting for — so the hold used
// to go out a second after the Stop landed, which is the opposite of what the
// press asked for. A stopped hold stays the reader's: it is kept, marked as
// not sent, and goes only if they say so.

import { afterEach, describe, expect, it, vi } from "vitest";
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

/** Who the queue is written down for. */
const ME = { userId: "usr_dana", orgId: "org_a" };
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
/** A SECOND turn, running and then ended, on top of the first. */
const RUNNING_AGAIN = [...FINISHED, userTurn("u2"), assistantTurn("a2")];
const FINISHED_AGAIN = [...FINISHED, userTurn("u2"), assistantTurn("a2", { completedAt: DONE })];

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

/** The turn ends, the way an ordinary end reads: the transcript settles and
 *  the box says it is idle. */
async function turnEnds(): Promise<void> {
  ds.turnState.mockReturnValue("idle");
  await live(FINISHED);
}

describe("a Stop while the composer is holding a message", () => {
  it("does not send the held message when the turn then ends", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");

    useChatStore.getState().cancel("c1");
    await turnEnds();
    // …and every later reconcile of the ended turn is the same answer.
    await turnEnds();

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    unsub();
  });

  it("keeps the words and marks them as not sent, for the reader to act on", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");

    useChatStore.getState().cancel("c1");
    await turnEnds();

    const [held] = useChatStore.getState().byId.c1.queued;
    expect(held.text).toBe("and then deploy it");
    expect(held.stopped).toBe(true);
    unsub();
  });

  it("cancels every held message, not just the one at the front", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "first");
    useChatStore.getState().queueMessage("c1", "second");

    useChatStore.getState().cancel("c1");
    await turnEnds();
    await turnEnds();

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.stopped)).toEqual([true, true]);
    unsub();
  });

  it("sends it once, and only once, when the reader asks for it", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    useChatStore.getState().cancel("c1");
    await turnEnds();

    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().sendQueued("c1", held.id);
    await flush();

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);

    await turnEnds();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    unsub();
  });

  it("holds a stopped message the reader resends mid-turn without re-stopping it", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    useChatStore.getState().cancel("c1");
    // The stop did not reach the box: the turn is still going when the reader
    // says to send the words anyway, so they wait for its end as any other
    // held message would.
    const [held] = useChatStore.getState().byId.c1.queued;
    useChatStore.getState().sendQueued("c1", held.id);
    await flush();
    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued[0].stopped).toBeFalsy();

    await turnEnds();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    unsub();
  });

  it("leaves nothing a reload of the tab would send by itself", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    useChatStore.getState().cancel("c1");
    await turnEnds();
    unsub();

    // A reload: nothing in memory survives, only what was written down.
    useChatStore.setState({ byId: {}, pending: {} });
    ds.getChatTurns.mockResolvedValue(FINISHED);
    ds.turnState.mockReturnValue("idle");
    const again = useChatStore.getState().open("c1");
    await flush();
    await live(FINISHED);

    expect(ds.sendUserMessage).not.toHaveBeenCalled();
    expect(useChatStore.getState().byId.c1.queued.map((q) => q.text)).toEqual([
      "and then deploy it",
    ]);
    again();
  });

  it("does not hold back a message typed AFTER the stop", async () => {
    const unsub = await openWorking();
    useChatStore.getState().cancel("c1");
    // The turn is still winding down, so the composer is still holding what is
    // typed — but this message was typed after the press, so nothing about it
    // was stopped and it goes on the turn's end like any other.
    useChatStore.getState().queueMessage("c1", "actually, try this instead");

    await turnEnds();

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("actually, try this instead");
    unsub();
  });
});

describe("an ordinary turn end", () => {
  it("still sends the held message when nobody pressed Stop", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");

    await turnEnds();

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    expect(useChatStore.getState().byId.c1.queued).toEqual([]);
    unsub();
  });

  it("is not tainted by a Stop the reader pressed on an EARLIER turn", async () => {
    const unsub = await openWorking();
    useChatStore.getState().cancel("c1");
    await turnEnds();

    // A new turn, started by the reader, with a message typed behind it.
    useChatStore.getState().send("c1", "next");
    await flush();
    ds.turnState.mockReturnValue("working");
    await live(RUNNING_AGAIN);
    ds.sendUserMessage.mockClear();
    useChatStore.getState().queueMessage("c1", "and then deploy it");

    ds.turnState.mockReturnValue("idle");
    await live(FINISHED_AGAIN);

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1);
    expect(ds.sendUserMessage.mock.calls[0]?.[1]).toBe("and then deploy it");
    unsub();
  });
});
