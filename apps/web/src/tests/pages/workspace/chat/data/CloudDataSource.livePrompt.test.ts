// A message a reader sent is ONE turn however the three tellings of it race.
//
// Three things say the same message, and no order between them is guaranteed:
// the `prompt` row the server writes when it accepts the send (read back over
// REST), the box's echo of the words on the live document, and the answer the
// box starts writing. On a reload they arrive in transcript order — prompt
// first — and the echo merges into the prompt's turn. Live they do not: the
// socket can deliver the echo and the first answer before the send's own
// response (or before the durable page a fresh reader is still fetching) has
// put the prompt on screen, and the message was then said twice — once as the
// box heard it, once more underneath the answer, still reading "Sending…".
//
// The contract here: one turn, whichever arrives first, and a turn a box has
// taken never keeps reading as still going out.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle } from "@/api/realtime/docSync";

const NOW = "2026-09-16T12:00:00Z";
const CHAT = "c1";

type DocMessage = { kind: string; payload?: unknown; ephemeral?: boolean; state?: unknown };

/** A document the test drives: every frame it pushes reaches the source. */
function scriptedDoc(): { handle: DocHandle<never>; push: (message: DocMessage) => void } {
  const listeners = new Set<(message: DocMessage) => void>();
  const handle = {
    onMessage: (listener: (message: DocMessage) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    onPhase: () => () => undefined,
    getPhase: () => ({
      phase: "live",
      epoch: 1,
      seq: 0,
      peerId: "p:1",
      canWrite: false,
      pending: 0,
      error: null,
    }),
    sendOp: () => Promise.reject(new Error("a reader never writes to a chat document")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
  return {
    handle,
    push: (message) => listeners.forEach((listener) => listener(message)),
  };
}

/** The row the server writes for a person's message — flat, no `event_type`. */
function promptRow(seq: number, clientId: string, text: string) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: "user" as const,
    kind: "prompt",
    event_id: `usr:${clientId}`,
    payload: {
      schema_version: "1.2.0",
      kind: "prompt",
      text,
      client_id: clientId,
      user_id: "u1",
      attachments: [],
      context: "",
    },
    created_at: NOW,
  };
}

/** A machine's transcript entry: the envelope, with the event one level in. */
function entryOf(role: "user" | "assistant", event: Record<string, unknown>) {
  const eventId = String(event.event_id);
  return { event_id: eventId, role, kind: String(event.event_type), payload: event };
}

function machineRow(seq: number, role: "user" | "assistant", event: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role,
    kind: String(event.event_type),
    event_id: String(event.event_id),
    payload: entryOf(role, event),
    created_at: NOW,
  };
}

/** What a harness publishes when it takes the person's message: it says the
 *  words back on a `user` message of its own. */
function echoEvents(text: string, messageId = "hm1"): Record<string, unknown>[] {
  return [
    {
      event_id: `${messageId}-created`,
      event_type: "message.created",
      message_id: messageId,
      role: "user",
    },
    {
      event_id: `${messageId}-part`,
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    },
  ];
}

function answerEvents(text: string, messageId = "a1"): Record<string, unknown>[] {
  return [
    {
      event_id: `${messageId}-created`,
      event_type: "message.created",
      message_id: messageId,
      role: "assistant",
    },
    {
      event_id: `${messageId}-part`,
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    },
  ];
}

/** An `append` op — how the box's events reach an open reader. */
function appendOp(events: Record<string, unknown>[], role: "user" | "assistant") {
  return {
    kind: "op",
    ephemeral: false,
    payload: {
      op_id: `op-${events[0]?.event_id as string}`,
      intent: "append",
      events: events.map((event) => entryOf(role, event)),
    },
  };
}

function sourceOver(pages: Record<string, unknown>[][]) {
  const listMessages = vi.fn();
  for (const items of pages) {
    listMessages.mockResolvedValueOnce({ items, next_after_seq: 999, resync_from: null });
  }
  listMessages.mockResolvedValue({ items: [], next_after_seq: 999, resync_from: null });
  const postMessage = vi.fn();
  const doc = scriptedDoc();
  return {
    listMessages,
    postMessage,
    push: doc.push,
    source: new CloudDataSource({
      rest: { listMessages, postMessage } as never,
      openDoc: () => doc.handle,
      acquire: () => () => undefined,
      clientId: () => "web-1",
    }),
  };
}

function userTexts(turns: { author: string; parts: { kind: string; text?: string }[] }[]): string[] {
  return turns
    .filter((turn) => turn.author === "user")
    .flatMap((turn) => turn.parts.filter((p) => p.kind === "text").map((p) => p.text ?? ""));
}

function userStatuses(turns: { author: string; status?: string }[]): (string | undefined)[] {
  return turns.filter((turn) => turn.author === "user").map((turn) => turn.status);
}

const SAID = "what is 6*7?";

describe("a sent message however the tellings of it race", () => {
  it("is one turn when the box's echo beats the send's own response back", async () => {
    // The server relays to the box before it answers the browser, so the echo
    // can arrive on the socket while the POST is still in flight.
    const { source, postMessage, push } = sourceOver([
      [promptRow(1, "web-1", SAID), ...echoEvents(SAID).map((e, i) => machineRow(2 + i, "user", e))],
    ]);
    let answerSend = (_: unknown) => undefined as void;
    postMessage.mockReturnValue(new Promise((resolve) => (answerSend = resolve)));
    source.subscribeChat(CHAT, () => undefined);

    const sent = source.sendUserMessage(CHAT, SAID);
    push(appendOp(echoEvents(SAID), "user"));
    push(appendOp(answerEvents("Answer: 42"), "assistant"));
    answerSend(promptRow(1, "web-1", SAID));
    await sent;

    const turns = await source.getChatTurns(CHAT);
    expect(userTexts(turns), "the message must not be said twice").toEqual([SAID]);
    expect(userStatuses(turns), "a box that echoed the words has taken them").toEqual(["done"]);
  });

  it("is one turn when the durable page lands after the live echo and answer", async () => {
    // A reader who never saw the send — a second tab, or the browser that has
    // just navigated into a freshly created chat: the socket is open and
    // streaming before the first REST page comes back.
    const { source, push } = sourceOver([
      [
        promptRow(1, "web-1", SAID),
        ...echoEvents(SAID).map((e, i) => machineRow(2 + i, "user", e)),
        ...answerEvents("Answer: 42").map((e, i) => machineRow(4 + i, "assistant", e)),
      ],
    ]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp(echoEvents(SAID), "user"));
    push(appendOp(answerEvents("Answer: 42"), "assistant"));
    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual([SAID]);
    expect(userStatuses(turns)).toEqual(["done"]);
    expect(JSON.stringify(turns)).toContain("Answer: 42");
  });

  it("stops reading as going out when the answer lands before the prompt row, unechoed", async () => {
    const { source, push } = sourceOver([
      [
        promptRow(1, "web-1", SAID),
        ...answerEvents("Answer: 42").map((e, i) => machineRow(2 + i, "assistant", e)),
      ],
    ]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp(answerEvents("Answer: 42"), "assistant"));
    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual([SAID]);
    expect(userStatuses(turns), "the machine is speaking, so the message was taken").toEqual([
      "done",
    ]);
  });

  it("still reads as going out when the prompt row is all the transcript holds", async () => {
    // The guard on the fix above: nothing has spoken, so nothing settles it.
    const { source, push } = sourceOver([[promptRow(1, "web-1", SAID)]]);
    source.subscribeChat(CHAT, () => undefined);
    push({ kind: "snapshot", state: { events: [], meta: {} } });

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual([SAID]);
    expect(userStatuses(turns)).toEqual(["running"]);
  });

  it("keeps two sends of the same words as two turns when only one was echoed", async () => {
    // The echo the box sent stands for the FIRST message; the second is still
    // going out and must not be folded into it.
    const { source, push } = sourceOver([
      [
        promptRow(1, "web-1", SAID),
        ...echoEvents(SAID).map((e, i) => machineRow(2 + i, "user", e)),
        promptRow(4, "web-2", SAID),
      ],
    ]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp(echoEvents(SAID), "user"));
    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual([SAID, SAID]);
    expect(userStatuses(turns)).toEqual(["done", "running"]);
  });
});
