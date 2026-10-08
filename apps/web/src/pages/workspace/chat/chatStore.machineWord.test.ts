// What a reader learns about their turn from the MACHINE — not from their tab.
//
// A cloud chat has as many readers as it has people, and each of them is one
// socket away from the box. Two things follow that a tab cannot work out on its
// own. A turn that ENDED while the socket was down is news the reopening frame
// carries and nothing else does — the transcript it replays is the one already
// on screen. And a Stop pressed in somebody ELSE's tab arrives here as the turn
// going from working to idle, which is exactly what an ordinary end looks like
// — and an ordinary end is what releases the words this composer is holding.
//
// Driven through the REAL `CloudDataSource`, with a scripted transport and
// document under it and nothing faked above it, because in both cases the
// defect is a word the source hears and never passes on.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useChatStore } from "./chatStore";
import { installChatRuntime, resetChatRuntime, type ChatHost } from "./data";
import { scriptedChat, type LogRow, type ScriptedChat } from "./data/convergence";
import { readQueued } from "./queuedMessages";

const CHAT = "chat";
/** Who the queue is written down for. */
const ME = { userId: "usr_dana", orgId: "org_a" };
/** The stamp a completion carries; without one a turn reads as still open. */
const DONE_AT = "2026-01-01T00:00:05Z";

/** One machine event, recorded in the log and put on the socket, the way the
 *  publisher writes it: the harness event one level inside the row's envelope. */
async function machineSays(
  rig: ScriptedChat,
  kind: string,
  event: Record<string, unknown>,
  role: LogRow["role"] = "assistant",
): Promise<void> {
  const seq = (rig.rows[rig.rows.length - 1]?.seq ?? 0) + 1;
  await rig.push([
    {
      id: `row-${seq}`,
      chat_id: CHAT,
      seq,
      role,
      kind,
      event_id: `evt-${seq}`,
      payload: { seq: null, kind, role, payload: { ...event, event_type: kind } },
      created_at: "2026-01-01T00:00:00Z",
    },
  ]);
}

/** The box answers the turn, to the last token. The transcript then owes
 *  nothing, so from here on only the machine's word holds the turn open. */
async function assistantAnswers(rig: ScriptedChat, id: string): Promise<void> {
  await machineSays(rig, "message.created", { role: "assistant", message_id: id });
  await machineSays(rig, "part.created", {
    part: { type: "text", text: "done", part_id: `${id}-p`, message_id: id },
  });
  await machineSays(rig, "message.completed", { message_id: id, time: DONE_AT });
}

/** Somebody else pressed Stop: the machine halts the turn, writes the note
 *  naming who did it, and goes idle. Nothing about any of it happened here. */
async function anotherReaderStops(rig: ScriptedChat, id: string): Promise<void> {
  await machineSays(
    rig,
    "session.status_changed",
    { status: "aborted", phase: "idle", turn_id: id, detail: null },
    "system",
  );
  await machineSays(rig, "message.created", { role: "system", message_id: `stop-${id}` }, "system");
  await machineSays(
    rig,
    "part.created",
    {
      part: {
        type: "text",
        text: "Stopped by Dana.",
        part_id: `stop-${id}-part`,
        message_id: `stop-${id}`,
        synthetic: true,
      },
    },
    "system",
  );
  await machineSays(
    rig,
    "message.completed",
    { message_id: `stop-${id}`, time: DONE_AT },
    "system",
  );
  await rig.setTurnState("idle");
}

let rig: ScriptedChat;
let close: (() => void) | null = null;

/** Open the chat the way the surface does: the store subscribes, and the
 *  socket's first frame answers. */
async function openChat(): Promise<void> {
  close = useChatStore.getState().open(CHAT);
  await rig.hello();
  await rig.settle();
}

/** The reader sends, and the box takes the turn. */
async function readerSends(text: string): Promise<void> {
  useChatStore.getState().send(CHAT, text);
  await rig.settle();
  await rig.setTurnState("working");
}

beforeEach(() => {
  rig = scriptedChat(CHAT);
  installChatRuntime({
    source: rig.source,
    host: { account: () => ({ email: null, webAppUrl: null, ...ME }) } as unknown as ChatHost,
  });
  useChatStore.setState({ byId: {}, pending: {}, composerPrefs: {} });
  try {
    localStorage.clear();
  } catch {
    // A browser that refuses the store leaves nothing to clear.
  }
});

afterEach(() => {
  close?.();
  close = null;
  resetChatRuntime();
});

describe("the socket comes back", () => {
  it("hands the composer back for a turn that ended while it was down", async () => {
    await openChat();
    await readerSends("go");
    await assistantAnswers(rig, "a1");
    // The transcript owes nothing now; the machine's word is the only thing
    // still holding the turn open.
    expect(useChatStore.getState().byId[CHAT].sending).toBe(true);

    // The socket drops and comes back. The box went idle while it was down, so
    // the reopening frame carries a word the replayed window cannot: every
    // entry in it is already on screen.
    await rig.hello({ turnState: "idle" });

    expect(rig.source.turnState(CHAT)).toBe("idle");
    expect(useChatStore.getState().byId[CHAT].sending).toBe(false);
  });

  it("leaves the last word standing when the frame says nothing about the turn", async () => {
    await openChat();
    await readerSends("go");
    await assistantAnswers(rig, "a1");

    // The document's meta is merged server-side and the box re-stamps the turn
    // on its own heartbeat, so a frame may carry other keys and nothing about
    // it. Silence is not the box saying the turn ended.
    await rig.hello();

    expect(rig.source.turnState(CHAT)).toBe("working");
    expect(useChatStore.getState().byId[CHAT].sending).toBe(true);
  });

  it("keeps a turn the box is still running", async () => {
    await openChat();
    await readerSends("go");
    await assistantAnswers(rig, "a1");

    await rig.hello({ turnState: "working" });

    expect(useChatStore.getState().byId[CHAT].sending).toBe(true);
  });
});

describe("a Stop pressed in another reader's tab", () => {
  it("cancels what this composer is holding instead of sending it", async () => {
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    await anotherReaderStops(rig, "t1");

    const [held] = useChatStore.getState().byId[CHAT].queued;
    expect(held.text).toBe("and then deploy it");
    expect(held.stopped).toBe(true);
    expect(rig.sent).toEqual([]);
  });

  it("cancels every held message, not just the one at the front", async () => {
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "first");
    useChatStore.getState().queueMessage(CHAT, "second");
    rig.sent.length = 0;

    await anotherReaderStops(rig, "t1");

    expect(useChatStore.getState().byId[CHAT].queued.map((q) => q.stopped)).toEqual([true, true]);
    expect(rig.sent).toEqual([]);
  });

  it("leaves nothing a reload of this tab would send by itself", async () => {
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    await anotherReaderStops(rig, "t1");

    expect(readQueued(ME, CHAT).map((held) => [held.text, held.stopped])).toEqual([
      ["and then deploy it", true],
    ]);
  });

  it("sends it when the reader asks for it after all", async () => {
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;
    await anotherReaderStops(rig, "t1");

    const [held] = useChatStore.getState().byId[CHAT].queued;
    useChatStore.getState().sendQueued(CHAT, held.id);
    await rig.settle();

    expect(rig.sent).toEqual(["and then deploy it"]);
  });

  it("does not cancel again when the socket comes back replaying the same Stop", async () => {
    await openChat();
    await readerSends("go");
    await anotherReaderStops(rig, "t1");
    // The reader says it anyway, in a turn of their own, and types the next
    // thing behind it.
    await readerSends("carry on");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    // The socket drops and comes back. The retained window it replays still
    // holds the Stop — the same one, not a second one.
    await rig.hello({ turnState: "working" });

    expect(useChatStore.getState().byId[CHAT].queued[0].stopped).toBeFalsy();
  });

  it("cancels again when a SECOND Stop lands", async () => {
    await openChat();
    await readerSends("go");
    await anotherReaderStops(rig, "t1");
    await readerSends("carry on");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    await anotherReaderStops(rig, "t2");

    expect(useChatStore.getState().byId[CHAT].queued[0].stopped).toBe(true);
    expect(rig.sent).toEqual([]);
  });

  it("does not cancel a message typed after a Stop the reader arrived to find", async () => {
    // The chat's record already holds a Stop when this reader opens it. That is
    // where the chat stands, not something that happened to them.
    await anotherReaderStops(rig, "t0");
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    await assistantAnswers(rig, "a1");
    await rig.setTurnState("idle");

    expect(rig.sent).toEqual(["and then deploy it"]);
  });
});

describe("a turn that ends on its own", () => {
  it("still sends the message the composer was holding", async () => {
    await openChat();
    await readerSends("go");
    useChatStore.getState().queueMessage(CHAT, "and then deploy it");
    rig.sent.length = 0;

    await assistantAnswers(rig, "a1");
    await rig.setTurnState("idle");

    expect(rig.sent).toEqual(["and then deploy it"]);
    expect(useChatStore.getState().byId[CHAT].queued).toEqual([]);
  });
});
