// The composer's busy state has no gaps.
//
// A turn is busy from the moment it starts until its terminal event. The
// transcript alone cannot say that: a compaction summary lands as the tail of
// the live turn (and an auto compaction is followed by a re-send of the very
// same prompt), so for a beat the folded transcript reads as owing nothing
// while the box is still working. The box says otherwise on its own lane —
// `turn_state: working`, re-stamped every 15 s — and that word is what keeps
// the composer from handing itself back mid-turn.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConversationTurn } from "@alkera/chat-model";

const { ds, subscribers } = vi.hoisted(() => {
  const subscribers: Array<(event: { replay?: boolean }) => void> = [];
  const ds = {
    getChatTurns: vi.fn(async () => [] as unknown[]),
    sendUserMessage: vi.fn(async () => ({ id: "m1", role: "user", content: "x" })),
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

vi.mock("./data", () => ({ chatData: () => ds, isSessionNotOpen: () => false }));

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

/** The shape an auto compaction leaves at the transcript tail: its own
 *  assistant turn whose only part is the summary. The turn it interrupted is
 *  re-sent straight after, so this is the middle of a turn, not its end. */
function compactionTurn(id: string): ConversationTurn {
  return {
    id,
    author: "assistant",
    status: "done",
    parts: [{ id: `${id}-c`, kind: "compaction", title: "Auto compaction", text: "summary" }],
  };
}

const DONE = "2026-09-05T00:00:00Z";

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  ds.getChatTurns.mockResolvedValue([]);
  ds.turnState.mockReturnValue(null);
});

/** Drive one live daemon event through the store. */
async function live(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({}));
  await flush();
}

describe("busy has no gap across a compaction", () => {
  it("stays busy while the compaction summary is the transcript tail", async () => {
    ds.getChatTurns.mockResolvedValue([]);
    const unsub = useChatStore.getState().open("c1");
    await flush();
    useChatStore.getState().send("c1", "go");
    await flush();
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The turn is under way and the box says so.
    ds.turnState.mockReturnValue("working");
    await live([userTurn("u1"), assistantTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The compaction lands mid-turn. The transcript now ends on a summary and
    // owes nothing by its own reading — the composer must NOT be handed back.
    await live([userTurn("u1"), assistantTurn("a1", { completedAt: DONE }), compactionTurn("k1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The harness re-sends the same prompt; the answer streams on.
    await live([
      userTurn("u1"),
      assistantTurn("a1", { completedAt: DONE }),
      compactionTurn("k1"),
      assistantTurn("a2"),
    ]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);
    unsub();
  });

  it("holds busy while the box re-stamps `working` over a settled transcript", async () => {
    const unsub = useChatStore.getState().open("c1");
    await flush();
    useChatStore.getState().send("c1", "go");
    await flush();

    ds.turnState.mockReturnValue("working");
    await live([userTurn("u1"), assistantTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // Between the compaction and the re-send every turn carries a completion:
    // the transcript reads finished while the box is still working.
    await live([userTurn("u1"), assistantTurn("a1", { completedAt: DONE })]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // Only the box's own end of turn hands the composer back.
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), assistantTurn("a1", { completedAt: DONE })]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub();
  });

  it("a settled transcript on a box that says nothing still ends the turn", async () => {
    // The editor's daemon publishes no turn state at all — its busy state is
    // the transcript's alone, exactly as before.
    ds.turnState.mockReturnValue(null);
    const unsub = useChatStore.getState().open("c1");
    await flush();
    useChatStore.getState().send("c1", "go");
    await flush();
    await live([userTurn("u1"), assistantTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);
    await live([userTurn("u1"), assistantTurn("a1", { completedAt: DONE })]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub();
  });
});
