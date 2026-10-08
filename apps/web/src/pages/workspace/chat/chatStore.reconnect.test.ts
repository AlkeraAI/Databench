// A socket that drops mid-turn is a gap in the news, not news.
//
// The composer is busy for exactly as long as the machine says it is working.
// When the chat's sockets are force-closed and reopened, the source's live word
// about the turn goes with them for a beat: the doc handle is re-helloed and the
// snapshot that re-states `turn_state` has not landed yet, so `turnState()`
// answers `null` — "nobody has said anything". Read as "the turn ended" that
// handed the composer back while a tool was still running on the box, and the
// reader's next prompt went into the turn that was still going.
//
// Silence is not the server's word. The last word stands until the machine
// replaces it.

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
  return { id, author: "user", status: "done", parts: [{ id: `${id}-t`, kind: "text", text: "hi" }] };
}

function assistantTurn(id: string): ConversationTurn {
  return { id, author: "assistant", status: "done", parts: [{ id: `${id}-t`, kind: "text", text: "…" }] };
}

/** What the reconnect's replay leaves at the tail: the tool that was running
 *  when the socket dropped is settled as failed and its turn stamped, so the
 *  folded transcript owes nothing by its own reading. */
function settledTurn(id: string): ConversationTurn {
  return {
    id,
    author: "assistant",
    status: "error",
    completedAt: "2026-09-21T00:00:00Z",
    parts: [{ id: `${id}-t`, kind: "text", text: "…" }],
  };
}

afterEach(() => {
  vi.clearAllMocks();
  subscribers.length = 0;
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  ds.getChatTurns.mockResolvedValue([]);
  ds.turnState.mockReturnValue(null);
});

/** One live frame from the box. */
async function live(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({}));
  await flush();
}

/** What a reopened socket delivers: a replay of the retained window, which is
 *  not live activity and is reconciled as history. */
async function replay(turns: ConversationTurn[]): Promise<void> {
  ds.getChatTurns.mockResolvedValue(turns);
  subscribers.forEach((cb) => cb({ replay: true }));
  await flush();
}

/** A chat with a turn under way and the box saying so. */
async function runningTurn(): Promise<() => void> {
  const unsub = useChatStore.getState().open("c1");
  await flush();
  useChatStore.getState().send("c1", "go");
  await flush();
  ds.turnState.mockReturnValue("working");
  await live([userTurn("u1"), assistantTurn("a1")]);
  expect(useChatStore.getState().byId.c1.sending).toBe(true);
  return unsub;
}

describe("a reconnect keeps the machine's last word", () => {
  it("stays busy while the reopened socket has not re-stated the turn", async () => {
    const unsub = await runningTurn();

    // The sockets are force-closed and reopen. The source has no live word
    // about the turn yet, and the replay settled the tool that was running.
    ds.turnState.mockReturnValue(null);
    await replay([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The snapshot lands and says the turn is still going: nothing changes.
    ds.turnState.mockReturnValue("working");
    await replay([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(true);

    // The box's own end of turn — and only that — hands the composer back.
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub();
  });

  it("a turn the box ended stays ended when the socket drops after it", async () => {
    // The remembered word is replaced by the machine, not accumulated: once the
    // box has said `idle`, a later gap in the news must not read as a turn.
    const unsub = await runningTurn();
    ds.turnState.mockReturnValue("idle");
    await live([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);

    ds.turnState.mockReturnValue(null);
    await replay([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c1.sending).toBe(false);
    unsub();
  });

  it("a source that never speaks still ends its turn on the transcript", async () => {
    // The editor's daemon publishes no turn state at all. With no word to
    // remember, the transcript stays the only authority it ever was.
    ds.turnState.mockReturnValue(null);
    const unsub = useChatStore.getState().open("c2");
    await flush();
    useChatStore.getState().send("c2", "go");
    await flush();
    await live([userTurn("u1"), assistantTurn("a1")]);
    expect(useChatStore.getState().byId.c2.sending).toBe(true);
    await live([userTurn("u1"), settledTurn("a1")]);
    expect(useChatStore.getState().byId.c2.sending).toBe(false);
    unsub();
  });
});
