// Transcript refreshes race. A send's failure refresh, the open replay, and each streamed
// daemon event all refetch the same chat, so several folds can be in flight at once and
// they do not come back in the order they left. Applying an older fold over a newer one
// rewrites the transcript from a state the chat has already left.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConversationTurn } from "@alkera/chat-model";

const { ds, subscribers, folds, rejects } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  /** Every getChatTurns call parks here as a resolver the test fires by hand. */
  const folds: Array<(turns: unknown[]) => void> = [];
  /** …and its rejecter, so a fetch can fail the way a dropped connection does. */
  const rejects: Array<(err: unknown) => void> = [];
  const ds = {
    getChatTurns: vi.fn(
      () =>
        new Promise<unknown[]>((resolve, reject) => {
          folds.push(resolve);
          rejects.push(reject);
        }),
    ),
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
    reopenChat: vi.fn(async () => undefined),
    subscribeChat: vi.fn((_id: string, cb: (event: { replay?: boolean }) => void) => {
      subscribers.push(cb);
      return () => {
        const i = subscribers.indexOf(cb);
        if (i >= 0) subscribers.splice(i, 1);
      };
    }),
  };
  return { ds, subscribers, folds, rejects };
});

vi.mock("./data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./data")>()),
  chatData: () => ds,
}));

import { useChatStore } from "./chatStore";

const OPTS = {
  model: { id: "m", displayName: "M", wire: "anthropic" as const, efforts: [], defaultEffort: null },
  effort: "high",
};
const REFUSED = { code: -32002, message: "session c1 is not open" };

const flush = async (): Promise<void> => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};

function userTurn(id: string, text: string): ConversationTurn {
  return { id, author: "user", status: "done", parts: [{ id: `${id}-t`, kind: "text", text }] };
}

function assistantTurn(id: string): ConversationTurn {
  return {
    id,
    author: "assistant",
    status: "done",
    completedAt: "2026-07-27T00:00:00Z",
    parts: [{ id: `${id}-t`, kind: "text", text: "ok" }],
  };
}

const entry = () => useChatStore.getState().byId.c1;
const turns = () => [...entry().base, ...entry().optimistic];
const bubblesSaying = (text: string): number =>
  turns().filter(
    (t) => t.author === "user" && t.parts.some((p) => p.kind === "text" && p.text === text),
  ).length;

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  folds.length = 0;
  rejects.length = 0;
  useChatStore.setState({ byId: {} });
});

describe("chatStore refresh sequencing", () => {
  it("keeps the newer fold when two fetches resolve out of order", async () => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    subscribers[0]({}); // a streamed event refetches: fetch 2
    await flush();
    expect(folds).toHaveLength(2);

    // The newer fetch lands first with two user turns, then the older one comes back
    // with the empty transcript the chat had before them.
    folds[1]([userTurn("u1", "first"), userTurn("u2", "second")]);
    await flush();
    folds[0]([]);
    await flush();

    // baseUserCount is what the next echo subtracts against, so a backwards snap here
    // makes the following echo retire no placeholder at all.
    expect(entry().baseUserCount).toBe(2);
    expect(turns().map((t) => t.id)).toEqual(["u1", "u2"]);
    unsub();
  });

  it("holds the resend until the reopened fold has applied", async () => {
    // The resend must be enqueued against the count the REOPENED session reports. The
    // refused attempt inflated it (the daemon recorded the try), and a placeholder
    // enqueued against that inflated count is one the echo can never retire.
    const unsub = useChatStore.getState().open("c1"); // fetch 1: the open replay
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // the refusal fires fetch 2
    folds[1]([userTurn("f1", "Hi"), assistantTurn("f2")]);
    await flush();
    expect(entry().baseUserCount).toBe(1); // inflated by the recorded attempt

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // reopenChat resolved and fired fetch 3, which is still in flight

    expect(ds.reopenChat).toHaveBeenCalledWith("c1");
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1); // still only the refused one
    expect(entry().optimistic).toHaveLength(0); // and no placeholder queued yet

    folds[2]([]); // the reopened session replays: the refused attempt was never persisted
    await flush();

    expect(entry().baseUserCount).toBe(0); // downshifted before the resend is queued
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2);
    unsub();
  });

  it("leaves one bubble for a resent message once the daemon echoes it", async () => {
    // The whole journey, with the open replay slow enough to land LAST: the failure
    // refresh, the reopened replay the resend waits on, the echo, and only then the
    // stale fetch 1, which must not rewrite any of it.
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();

    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2
    expect(entry().staleSend).toEqual({ text: "Hi", opts: OPTS });
    folds[1]([userTurn("f1", "Hi"), assistantTurn("f2")]);
    await flush();

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3
    folds[2]([]);
    await flush();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2);

    // The daemon echoes the resent message: fetch 4.
    subscribers[0]({});
    await flush();
    folds[3]([userTurn("u1", "Hi"), assistantTurn("a1")]);
    await flush();

    // The open replay finally comes back with the transcript from before any of this.
    folds[0]([]);
    await flush();

    // One message the user typed once, delivered once: one bubble.
    expect(bubblesSaying("Hi")).toBe(1);
    expect(entry().base.map((t) => t.id)).toEqual(["u1", "a1"]);
    expect(entry().optimistic).toHaveLength(0);
    unsub();
  });

  it("brings the prompt back when the reopen refresh fails", async () => {
    // A refresh that REJECTS proves nothing about the count, so there is no honest way to
    // enqueue against it. Sending anyway risks a bubble the echo can never retire; giving
    // up loses the user's words. The prompt returns and their one action still works.
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2
    folds[1]([userTurn("f1", "Hi"), assistantTurn("f2")]);
    await flush();

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3, the reopened replay
    rejects[2](new Error("the daemon went away mid-replay"));
    await flush();

    expect(ds.sendUserMessage).toHaveBeenCalledTimes(1); // no blind resend
    expect(entry().staleSend).toEqual({ text: "Hi", opts: OPTS }); // the prompt is back
    expect(bubblesSaying("Hi")).toBe(1); // the recorded attempt, not a duplicate

    // And the prompt still works: a second attempt whose replay APPLIES goes through.
    useChatStore.getState().reopenAndResend("c1");
    await flush();
    folds[3]([]);
    await flush();
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2);
    unsub();
  });

  it("clears the refused send's placeholder before resending", async () => {
    // The interleaving where the reopened replay lands while the FAILURE fold is still in
    // flight, so the count never inflated. The refused send's placeholder is still on
    // screen, and the replay is authoritative that the daemon holds no such turn: it goes,
    // and the resend's own bubble is the only one the echo has to retire.
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2, deliberately left pending
    expect(entry().optimistic).toHaveLength(1); // the refused send's bubble

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3
    folds[2]([]); // the reopened session reports zero user turns
    await flush();

    expect(entry().optimistic).toHaveLength(1); // the resend's, not two stacked up
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2);

    subscribers[0]({}); // the echo: fetch 4
    await flush();
    folds[3]([userTurn("u1", "Hi"), assistantTurn("a1")]);
    await flush();
    folds[1]([]); // the failure fold finally lands, far too late to matter
    await flush();

    expect(bubblesSaying("Hi")).toBe(1);
    expect(entry().optimistic).toHaveLength(0);
    unsub();
  });
});

// The reopen refresh is a real round trip, and the user can keep typing through it. What
// the resend clears has to be scoped to what was on screen when they clicked, and any
// prompt raised DURING the window belongs to a different message than the one being resent.
describe("chatStore reopenAndResend during a live window", () => {
  it("spares a placeholder enqueued while the refresh was pending", async () => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2, left pending
    expect(entry().optimistic).toHaveLength(1);

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3 in flight; the click captured one leftover

    // The user types again while the reopen is still going.
    useChatStore.getState().send("c1", "And this", OPTS);
    await flush();
    expect(entry().optimistic).toHaveLength(2);

    folds[2]([]); // the reopened replay applies
    await flush();

    // The leftover went, the newer send's bubble stayed, and the resend added its own.
    const texts = entry().optimistic.flatMap((t) =>
      t.parts.filter((p) => p.kind === "text").map((p) => p.text),
    );
    expect(texts).toEqual(["And this", "Hi"]);

    // Both echo normally: two user turns land and both placeholders retire.
    subscribers[0]({});
    await flush();
    folds[3]([userTurn("u1", "And this"), userTurn("u2", "Hi"), assistantTurn("a1")]);
    await flush();
    expect(entry().optimistic).toHaveLength(0);
    expect(bubblesSaying("Hi")).toBe(1);
    expect(bubblesSaying("And this")).toBe(1);
    unsub();
  });

  it("keeps a prompt raised during the window rather than resending", async () => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3 in flight

    // The newer send is refused too, raising a prompt for ITS text.
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "And this", OPTS);
    await flush();
    expect(entry().staleSend).toEqual({ text: "And this", opts: OPTS });

    folds[2]([]); // the older message's resend goes through
    await flush();

    // The surviving prompt names the message still undelivered, never the resent one.
    expect(entry().staleSend).toEqual({ text: "And this", opts: OPTS });
    unsub();
  });

  it("keeps the newer prompt when the resend is refused again", async () => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3 in flight

    // The newer send is refused during the window, raising a prompt for ITS text.
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "And this", OPTS);
    await flush(); // fetch 4
    expect(entry().staleSend).toEqual({ text: "And this", opts: OPTS });

    // The imminent resend of "Hi" will be refused too.
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    folds[2]([]); // the reopened fold applies and the resend fires
    await flush();

    // The re-refusal re-offers the resend's ORIGINAL record, whose recency predates
    // the prompt raised during the window. Minting a fresh record here would stamp
    // the OLD message as newest and evict the newer prompt from the one slot.
    expect(entry().staleSend).toEqual({ text: "And this", opts: OPTS });
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(3);
    unsub();
  });

  it("keeps the newer prompt when the reopen refresh rejects", async () => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "Hi", OPTS);
    await flush(); // fetch 2

    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3

    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "And this", OPTS);
    await flush();

    rejects[2](new Error("the daemon went away mid-replay"));
    await flush();

    // Restoring the older message here would overwrite the newer prompt and lose it:
    // the failure path may only fill a prompt that is EMPTY.
    expect(entry().staleSend).toEqual({ text: "And this", opts: OPTS });
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2); // no resend of the older one
    unsub();
  });
});

// There is ONE prompt slot and two reopen windows can overlap, so which refusal it keeps
// cannot depend on which network call happens to fail last. It keeps the newest.
describe("chatStore two overlapping reopen windows", () => {
  it.each([
    ["the older rejection lands last", [0, 1] as const],
    ["the newer rejection lands last", [1, 0] as const],
  ])("keeps the newer refusal when %s", async (_label, order) => {
    const unsub = useChatStore.getState().open("c1"); // fetch 1
    await flush();

    // A is refused, and the user clicks its prompt.
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "A", OPTS);
    await flush(); // fetch 2
    expect(entry().staleSend).toEqual({ text: "A", opts: OPTS });
    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 3: A's reopen refresh
    const aRefresh = 2;

    // During A's window, B is refused too, and the user clicks ITS prompt.
    ds.sendUserMessage.mockRejectedValueOnce(REFUSED);
    useChatStore.getState().send("c1", "B", OPTS);
    await flush(); // fetch 4
    expect(entry().staleSend).toEqual({ text: "B", opts: OPTS });
    useChatStore.getState().reopenAndResend("c1");
    await flush(); // fetch 5: B's reopen refresh
    const bRefresh = 4;

    // Both reopens fail, in whichever order the network chose.
    const pair = [aRefresh, bRefresh] as const;
    for (const which of order) {
      rejects[pair[which]](new Error(`refresh ${which} died`));
      await flush();
    }

    // B is the newer refusal, so B is what the one slot holds. Resolution order does not
    // enter into it: a last-writer-wins slot would read "A" on one of these two runs.
    expect(entry().staleSend).toEqual({ text: "B", opts: OPTS });
    // Neither message was resent, so nothing was silently delivered twice.
    expect(ds.sendUserMessage).toHaveBeenCalledTimes(2);
    unsub();
  });
});
