// A refused send is held in ONE place.
//
// The composer's own send keeps its refusal as an armed retry: the words went
// back to a field and nothing else is holding them. Every other caller hands
// its words back to something the reader acts on — the queue's own row, the
// reopen prompt's record — and arms nothing, because the reader's click on
// that IS the retry.
//
// Held in both at once is a message in two places. A queued row put back and a
// timer armed over it meant the retry landed the message in the transcript
// while the row stayed in the queue, under a live Send key and written into
// this browser, so one click sent it a second time: asked of the model twice,
// billed twice, in the chat twice. A second refusal wrote a second row
// answering to the SAME id, which one React key draws and which an edit or a
// removal acts on both of. And the reader's own Send, diverted into the queue
// behind that row, never cancelled the timer — so the words that went were the
// old ones and the words they pressed Send on sat waiting.
//
// Driven through the real store: a real `open`, the real subscription's
// reconcile, a real `ApiError` carrying real headers, and the browser's own
// copy of the queue read back at every step.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationTurn } from "@alkera/chat-model";

import { ApiError } from "@/api/errors";

const { ds, subscribers, wire } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const wire = {
    sends: [] as string[],
    answer: (async () => ({ id: "m1" })) as (attempt: number) => Promise<unknown>,
  };
  const ds = {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn((_chatId: string, text: string) => {
      wire.sends.push(text);
      return wire.answer(wire.sends.length - 1);
    }),
    turnState: vi.fn((_id: string) => null as "working" | "idle" | null),
    stoppedTurn: vi.fn((_id: string) => null as string | null),
    subscribeChat: vi.fn((_id: string, cb: (event: { replay?: boolean }) => void) => {
      subscribers.push(cb);
      return () => {
        const at = subscribers.indexOf(cb);
        if (at >= 0) subscribers.splice(at, 1);
      };
    }),
  };
  return { ds, subscribers, wire };
});

/** Who the queue is written down for. */
const ME = { userId: "usr_dana", orgId: "org_a" };
vi.mock("./data", () => ({
  chatData: () => ds,
  chatHost: () => ({ account: () => ({ email: null, webAppUrl: null, ...ME }) }),
  isSessionNotOpen: () => false,
}));

import { useChatStore } from "./chatStore";
import { readQueued, writeQueued } from "./queuedMessages";

/** A real 429 as the transport builds one, with `Retry-After` on real headers. */
function throttle(retryAfter: string): ApiError {
  return new ApiError(
    429,
    { detail: { code: "rate_limited", message: "You are sending messages very quickly." } },
    "the request to /api/v1/chats/c1/messages failed",
    new Headers({ "retry-after": retryAfter }),
  );
}

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

const DONE = "2026-09-21T00:00:00Z";
const RUNNING = [userTurn("u1"), assistantTurn("a1")];
const FINISHED = [userTurn("u1"), assistantTurn("a1", { completedAt: DONE })];

const entry = () => useChatStore.getState().byId.c1;
const queuedTexts = (): string[] => entry().queued.map((held) => held.text);

/** Let the floated `.then` handlers run without moving the clock. */
const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(0);
};

/** A streamed frame: the transcript the source now holds, reconciled live. */
async function live(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({}));
  await settle();
}

/** Open a chat with a turn already running, so a message typed now is held. */
async function openWorking(): Promise<() => void> {
  const unsub = useChatStore.getState().open("c1");
  await settle();
  useChatStore.getState().send("c1", "go");
  await settle();
  ds.turnState.mockReturnValue("working");
  await live(RUNNING);
  wire.sends.length = 0;
  return unsub;
}

beforeEach(() => {
  vi.useFakeTimers();
  // The retry's spread is pinned, so a wait that IS armed is exactly the one
  // the server named and the clock can be wound to it.
  vi.spyOn(Math, "random").mockReturnValue(0);
  wire.sends.length = 0;
  wire.answer = async () => ({ id: "m1" });
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, pending: {} });
  ds.getChatTurns.mockResolvedValue([]);
  ds.turnState.mockReturnValue(null);
  ds.stoppedTurn.mockReturnValue(null);
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("a queued message the limiter refuses", () => {
  it("goes back to the queue and nothing sends it again by itself", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    const id = entry().queued[0].id;

    // The turn ends and the queue releases it — straight into a 429.
    wire.answer = async () => {
      throw throttle("3");
    };
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);

    expect(wire.sends).toEqual(["and then deploy it"]);
    expect(entry().queued).toHaveLength(1);
    expect(entry().queued[0].id).toBe(id);
    expect(entry().queued[0].restored).toBe(true);
    expect(readQueued(ME, "c1").map((held) => held.text)).toEqual(["and then deploy it"]);

    // From here the limiter would take it. Nothing may take it: the row says
    // it waits for the reader, and a timer that sent it anyway would put the
    // message in the transcript with the row still offering to send it again.
    wire.answer = async () => ({ id: "m1" });
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();

    expect(wire.sends).toEqual(["and then deploy it"]);
    expect(entry().optimistic).toHaveLength(0);
    expect(queuedTexts()).toEqual(["and then deploy it"]);
    // Nothing is coming, so the line says the send is the reader's to make.
    expect(entry().sendRefusal).toBe("Sending too quickly. Try again in a moment.");

    // The reader's click is the retry — and once it lands the queue is empty,
    // so the message is in the chat and nowhere else.
    useChatStore.getState().sendQueued("c1", id);
    await settle();

    expect(wire.sends).toEqual(["and then deploy it", "and then deploy it"]);
    expect(entry().queued).toEqual([]);
    expect(readQueued(ME, "c1")).toEqual([]);
    unsub();
  });

  it("does the same when the reader released it by hand", async () => {
    // A row waiting from a previous page session: written down, read back on
    // open, and sent because the reader pressed its key.
    writeQueued(ME, "c1", [{ id: "q1", text: "the queued one" }]);
    const unsub = useChatStore.getState().open("c1");
    await settle();
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    expect(queuedTexts()).toEqual(["the queued one"]);

    wire.answer = async () => {
      throw throttle("3");
    };
    useChatStore.getState().sendQueued("c1", "q1");
    await settle();

    expect(wire.sends).toEqual(["the queued one"]);
    expect(entry().queued).toHaveLength(1);
    expect(entry().queued[0].id).toBe("q1");
    expect(entry().queued[0].restored).toBe(true);
    expect(readQueued(ME, "c1").map((held) => held.text)).toEqual(["the queued one"]);

    wire.answer = async () => ({ id: "m1" });
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();

    expect(wire.sends).toEqual(["the queued one"]);
    expect(queuedTexts()).toEqual(["the queued one"]);
    unsub();
  });

  it("goes back where it was standing, so nothing typed after it goes first", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "first run the tests");
    useChatStore.getState().queueMessage("c1", "then deploy it");
    const [first, second] = entry().queued.map((held) => held.id);

    // The turn ends, the queue releases the one in front, and it is refused.
    wire.answer = async () => {
      throw throttle("3");
    };
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);

    expect(wire.sends).toEqual(["first run the tests"]);
    expect(queuedTexts()).toEqual(["first run the tests", "then deploy it"]);
    expect(entry().queued.map((held) => held.id)).toEqual([first, second]);
    expect(readQueued(ME, "c1").map((held) => held.text)).toEqual([
      "first run the tests",
      "then deploy it",
    ]);

    // The limiter would take one now, and the queue releases whatever is in
    // front. The refused message is still in front, and it waits for a click —
    // so it holds the one typed after it rather than letting it past.
    wire.answer = async () => ({ id: "m1" });
    await live(FINISHED);
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();

    expect(wire.sends).toEqual(["first run the tests"]);
    expect(queuedTexts()).toEqual(["first run the tests", "then deploy it"]);

    // The reader's click sends theirs, and only then is the other one free.
    useChatStore.getState().sendQueued("c1", first);
    await settle();

    expect(wire.sends).toEqual(["first run the tests", "first run the tests"]);
    expect(queuedTexts()).toEqual(["then deploy it"]);
    unsub();
  });

  it("is one row after a second refusal, never two answering to one id", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "and then deploy it");
    const id = entry().queued[0].id;

    wire.answer = async () => {
      throw throttle("2");
    };
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);
    // Long enough for a second refusal to have landed, had anything re-sent it.
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();

    const ids = entry().queued.map((held) => held.id);
    expect(ids).toEqual([id]);
    expect(new Set(ids).size).toBe(ids.length);
    expect(readQueued(ME, "c1")).toHaveLength(1);
    unsub();
  });
});

describe("the reader's own Send while a retry is armed", () => {
  it("cancels it even when their words join the queue behind a held message", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "held from before");

    // Their own send, refused: the words come back to the field and a retry is
    // armed. The composer's refusal writes no queue row — the field is where
    // its words are, and a row would head-block everything typed after it.
    wire.answer = async () => {
      throw throttle("3");
    };
    useChatStore.getState().send("c1", "the first words");
    await settle();
    expect(entry().returnedDraft?.text).toBe("the first words");
    expect(queuedTexts()).toEqual(["held from before"]);

    // Somebody else stops the turn, which cancels what this browser was
    // holding: the head no longer goes on its own, so what the reader sends
    // next joins the queue behind it.
    ds.stoppedTurn.mockReturnValue("stop-1");
    await live(RUNNING);
    expect(entry().queued[0].stopped).toBe(true);

    wire.sends.length = 0;
    useChatStore.getState().send("c1", "my own words");
    await settle();
    expect(queuedTexts()).toEqual(["held from before", "my own words"]);

    // Their press is their word on what this chat sends next. Nothing may go
    // out over it — least of all the words they replaced.
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();

    expect(wire.sends).toEqual([]);
    expect(queuedTexts()).toEqual(["held from before", "my own words"]);
    unsub();
  });
});

describe("a send of other words while the composer's retry waits", () => {
  it("leaves it armed, so the words in the field still go", async () => {
    const unsub = await openWorking();
    useChatStore.getState().queueMessage("c1", "the waiting row");

    // Their own send, refused: the words are back in the field and the line
    // under it says they are going again in three seconds.
    wire.answer = async () => {
      throw throttle("3");
    };
    useChatStore.getState().send("c1", "typed now");
    await settle();

    expect(wire.sends).toEqual(["typed now"]);
    expect(entry().returnedDraft?.text).toBe("typed now");
    expect(entry().sendRefusal).toBe("Sending too quickly. Retrying in 3 seconds.");

    // The turn ends and the queue releases its row, which the limiter takes.
    wire.answer = async () => ({ id: "m1" });
    ds.turnState.mockReturnValue("idle");
    await live(FINISHED);

    expect(wire.sends).toEqual(["typed now", "the waiting row"]);

    // A message that is not theirs going out is not the reader taking back the
    // send they were promised. Theirs was never recorded and nothing but the
    // field is holding it, so the wait it was given still ends in a send.
    await vi.advanceTimersByTimeAsync(4_000);
    await settle();

    expect(wire.sends).toEqual(["typed now", "the waiting row", "typed now"]);
    unsub();
  });
});
