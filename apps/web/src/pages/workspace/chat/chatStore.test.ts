// The working indicator's lifecycle across navigation. `sending` must survive
// leaving and re-entering a chat whose turn is still streaming (the singleton
// store remembers the turn was initiated HERE), while a cold open of an
// abandoned chat must stay idle — non-live reconciles may keep-or-clear, never
// resurrect.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConversationTurn } from "@alkera/chat-model";

const { ds, subscribers } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const ds = {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
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

vi.mock("./data", () => ({ chatData: () => ds }));

import { DRAFT_CHAT_KEY, useChatStore } from "./chatStore";

const flush = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

function userTurn(id: string): ConversationTurn {
  return { id, author: "user", status: "done", parts: [{ id: `${id}-t`, kind: "text", text: "hi" }] };
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

/** A transcript whose last turn is still in flight (assistant, no completedAt). */
const IN_FLIGHT = [userTurn("u1"), assistantTurn("a1")];
/** A finished transcript. */
const FINISHED = [userTurn("u1"), assistantTurn("a1", { completedAt: "2026-06-11T00:00:00Z" })];

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, composerPrefs: {} });
  ds.getChatTurns.mockResolvedValue([]);
});

describe("chatStore working indicator", () => {
  it("keeps `sending` across leave-and-return while the turn streams", async () => {
    ds.getChatTurns.mockResolvedValue(IN_FLIGHT);
    const store = useChatStore.getState();

    const unsub = store.open("c1");
    await flush();
    // The user sends here, so this webview owns the turn.
    store.send("c1", "hi");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // Navigate away (unsubscribe) and back (re-open). The non-live history
    // reconcile must NOT wipe the indicator while the transcript still awaits
    // a response.
    unsub();
    const unsub2 = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(true);
    unsub2();
  });

  it("stays idle on a cold open of an in-flight transcript", async () => {
    // No prior entry — e.g. an extension reload. The last turn never finished,
    // but this webview never initiated it, so the indicator must stay off.
    ds.getChatTurns.mockResolvedValue(IN_FLIGHT);
    const unsub = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub();
  });

  it("clears `sending` on return when the turn finished while away", async () => {
    ds.getChatTurns.mockResolvedValue(IN_FLIGHT);
    const store = useChatStore.getState();
    const unsub = store.open("c1");
    await flush();
    store.send("c1", "hi");
    unsub();

    // The daemon finished the turn while no one was subscribed.
    ds.getChatTurns.mockResolvedValue(FINISHED);
    const unsub2 = useChatStore.getState().open("c1");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub2();
  });

  it("clears a never-echoed send on re-entry", async () => {
    // The send was optimistic but the daemon never accepted it — the folded
    // base still ends on the old FINISHED turn, so re-entry must clear both
    // the phantom bubble and the indicator.
    ds.getChatTurns.mockResolvedValue(FINISHED);
    const store = useChatStore.getState();
    const unsub = store.open("c1");
    await flush();
    store.send("c1", "lost");
    unsub();

    const unsub2 = useChatStore.getState().open("c1");
    await flush();
    const entry = useChatStore.getState().byId.c1;
    expect(entry.sending).toBe(false);
    expect(entry.optimistic).toEqual([]);
    unsub2();
  });

  it("a live event re-derives `sending` from the transcript", async () => {
    ds.getChatTurns.mockResolvedValue([userTurn("u1")]);
    const store = useChatStore.getState();
    const unsub = store.open("c1");
    await flush();
    store.send("c1", "hi");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The daemon echoes the sent message and completes its reply — the live
    // reconcile retires the optimistic bubble and drops the indicator.
    ds.getChatTurns.mockResolvedValue([
      userTurn("u1"),
      userTurn("u2"),
      assistantTurn("a2", { completedAt: "2026-06-11T00:00:01Z" }),
    ]);
    subscribers.forEach((cb) => cb({ replay: false }));
    await flush();
    const entry = useChatStore.getState().byId.c1;
    expect(entry.sending).toBe(false);
    expect(entry.optimistic).toEqual([]);
    unsub();
  });
});

// The composer-prefs slice: the only home a chat's mode/model/effort picks have
// between mounts (the daemon owns the durable copy; this keeps the pill and
// pickers steady across navigation), and the ONLY home a draft chat has at all.
describe("chatStore composer prefs", () => {
  it("keeps per-chat prefs across leave-and-return", async () => {
    const store = useChatStore.getState();
    store.setComposerPref("c1", { mode: "plan" });
    store.setComposerPref("c1", { model: "gpt-5.5" });
    store.setComposerPref("c1", { effort: "high" });
    store.setComposerPref("c2", { mode: "auto" });
    store.setComposerPref(DRAFT_CHAT_KEY, { model: "claude-opus-4-8" });

    // Leave and return: an open/unsubscribe/open cycle on c1.
    const unsub = store.open("c1");
    await flush();
    unsub();
    const unsub2 = useChatStore.getState().open("c1");
    await flush();
    unsub2();

    // Patches accumulated per key; neither the neighbor chat nor the draft bled.
    expect(useChatStore.getState().composerPrefs).toEqual({
      c1: { mode: "plan", model: "gpt-5.5", effort: "high" },
      c2: { mode: "auto" },
      [DRAFT_CHAT_KEY]: { model: "claude-opus-4-8" },
    });
  });

  it("a re-pick overwrites only its own field", () => {
    // Each picker patches one field; switching the model must not reset the
    // mode or effort picked earlier on the same chat.
    const store = useChatStore.getState();
    store.setComposerPref("c1", { mode: "plan", model: "gpt-5.5", effort: "high" });
    store.setComposerPref("c1", { model: "claude-opus-4-8" });

    expect(useChatStore.getState().composerPrefs.c1).toEqual({
      mode: "plan",
      model: "claude-opus-4-8",
      effort: "high",
    });
  });

  it("the first claim takes the draft's choices and consumes it", () => {
    // Two chats created back-to-back: only the FIRST inherits the draft. Were
    // the draft key merely copied, every later chat would silently open on the
    // previous draft's choices.
    const store = useChatStore.getState();
    store.setComposerPref(DRAFT_CHAT_KEY, { mode: "plan", model: "gpt-5.5", effort: "high" });

    store.claimDraftPrefs("c9");
    store.claimDraftPrefs("c10");

    const prefs = useChatStore.getState().composerPrefs;
    expect(prefs.c9).toEqual({ mode: "plan", model: "gpt-5.5", effort: "high" });
    expect("c10" in prefs).toBe(false);
    expect(DRAFT_CHAT_KEY in prefs).toBe(false);
  });

  it("claimDraftPrefs fills gaps only, never overriding the chat's own", () => {
    // The daemon's pushed mode can land on the created chat before the claim;
    // the draft must not roll it back, while its other fields still transfer.
    const store = useChatStore.getState();
    store.setComposerPref("c9", { mode: "auto" });
    store.setComposerPref(DRAFT_CHAT_KEY, { mode: "plan", model: "gpt-5.5" });

    store.claimDraftPrefs("c9");

    expect(useChatStore.getState().composerPrefs.c9).toEqual({ mode: "auto", model: "gpt-5.5" });
  });

  it("claimDraftPrefs without a draft leaves the store untouched", () => {
    const store = useChatStore.getState();
    store.setComposerPref("c1", { mode: "plan" });
    const before = useChatStore.getState().composerPrefs;

    store.claimDraftPrefs("c9");

    // Same object — no rewrite, no phantom entry for the claimed chat.
    expect(useChatStore.getState().composerPrefs).toBe(before);
    expect("c9" in before).toBe(false);
  });
});
