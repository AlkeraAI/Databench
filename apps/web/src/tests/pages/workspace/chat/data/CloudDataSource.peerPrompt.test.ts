// A message ANOTHER reader sent reaches an open chat live.
//
// The server relays every accepted send on the chat's document as a
// `user_message` op — the same lane the machine's stream rides — so a second
// tab, or a colleague watching the same chat, is told the moment someone
// speaks. The viewer folded the machine's `append` and `chunk` ops and dropped
// the relay on the floor, so the other reader saw "Working…" over a transcript
// missing the message it was working on, until a hard refresh re-read the
// durable page.
//
// The contract here: the relayed message renders without a transcript refetch,
// once, and the sender's own tab — which receives the same relay beside its
// send's own response — still shows it exactly once.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle } from "@/api/realtime/docSync";

const NOW = "2026-09-16T12:00:00Z";
const CHAT = "c1";

type DocMessage = { kind: string; payload?: unknown; ephemeral?: boolean; state?: unknown };

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

/** The relay the server puts on the document the moment it accepts a send —
 *  seq 0, the unsequenced lane, so the socket hands it over as ephemeral. */
function promptRelayOp(seq: number, clientId: string, text: string, userId = "u2") {
  return {
    kind: "op",
    ephemeral: true,
    payload: {
      op_id: `srv-${seq}`,
      intent: "user_message",
      events: [
        {
          schema_version: "1.2.0",
          kind: "prompt",
          message_id: `row-${seq}`,
          seq,
          text,
          client_id: clientId,
          user_id: userId,
          attachments: [],
          context: "",
        },
      ],
    },
  };
}

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

const SAID = "hi";

describe("a message another reader sent", () => {
  it("renders live, once, without a transcript refetch", async () => {
    // Tab two: the chat is open and caught up, nothing pending locally.
    const { source, listMessages, push } = sourceOver([[]]);
    const told: string[] = [];
    source.subscribeChat(CHAT, (event) => told.push(event.kind));
    expect(userTexts(await source.getChatTurns(CHAT))).toEqual([]);
    const reads = listMessages.mock.calls.length;

    push(promptRelayOp(1, "web-other", SAID));

    expect(told, "the reader is told the conversation moved").toContain("graph_changed");
    const turns = await source.getChatTurns(CHAT);
    expect(userTexts(turns)).toEqual([SAID]);
    expect(userStatuses(turns), "nothing has taken it yet").toEqual(["running"]);
    expect(listMessages.mock.calls.length, "no durable re-read was needed").toBe(reads);
  });

  it("is settled in place by the box's echo, and stays one turn across the durable page", async () => {
    const { source, push } = sourceOver([
      [],
      [promptRow(1, "web-other", SAID), ...echoEvents(SAID).map((e, i) => machineRow(2 + i, "user", e))],
    ]);
    source.subscribeChat(CHAT, () => undefined);
    await source.getChatTurns(CHAT);

    push(promptRelayOp(1, "web-other", SAID));
    push(appendOp(echoEvents(SAID), "user"));
    let turns = await source.getChatTurns(CHAT);
    expect(userTexts(turns)).toEqual([SAID]);
    expect(userStatuses(turns)).toEqual(["done"]);

    // A reconnect: the snapshot re-reads the durable page, which carries the
    // same message under the same id.
    push({ kind: "snapshot", state: { events: [], meta: {} } });
    await vi.waitFor(async () => {
      turns = await source.getChatTurns(CHAT);
      expect(userTexts(turns)).toEqual([SAID]);
    });
    expect(userStatuses(turns)).toEqual(["done"]);
  });

  it("is the sender's own message once, whether the relay lands before or after the send's response", async () => {
    for (const relayFirst of [true, false]) {
      const { source, postMessage, push } = sourceOver([[]]);
      let answerSend = (_: unknown) => undefined as void;
      postMessage.mockReturnValue(new Promise((resolve) => (answerSend = resolve)));
      source.subscribeChat(CHAT, () => undefined);
      await source.getChatTurns(CHAT);

      const sent = source.sendUserMessage(CHAT, SAID);
      if (relayFirst) push(promptRelayOp(1, "web-1", SAID, "u1"));
      answerSend(promptRow(1, "web-1", SAID));
      await sent;
      if (!relayFirst) push(promptRelayOp(1, "web-1", SAID, "u1"));

      const turns = await source.getChatTurns(CHAT);
      expect(userTexts(turns), `relay ${relayFirst ? "before" : "after"} the response`).toEqual([SAID]);
      expect(userStatuses(turns)).toEqual(["running"]);
    }
  });

  it("is not a turn when the relay is something the server minted for the box", async () => {
    const { source, push } = sourceOver([[]]);
    source.subscribeChat(CHAT, () => undefined);
    await source.getChatTurns(CHAT);

    push({
      kind: "op",
      ephemeral: true,
      payload: {
        op_id: "srv-rq",
        intent: "user_message",
        events: [{ kind: "run_query", node_id: "n1", text: SAID, client_id: "web-other" }],
      },
    });

    expect(userTexts(await source.getChatTurns(CHAT))).toEqual([]);
  });
});

describe("a permission mode another reader switched", () => {
  it("reaches this reader's pill live, and a message beside it still folds", async () => {
    // The route records the switch on the chat and mints a `mode` relay on the
    // same lane a person's message rides. The box does not re-stamp the mode
    // on the document's meta, so the relay is the only live word another tab
    // gets about it.
    const { source, push } = sourceOver([[]]);
    const modes: string[] = [];
    source.subscribeChat(CHAT, () => undefined);
    source.subscribePermissionMode(CHAT, (mode) => modes.push(mode));
    await source.getChatTurns(CHAT);

    push({
      kind: "op",
      ephemeral: true,
      payload: {
        op_id: "srv-mode",
        intent: "user_message",
        events: [
          { schema_version: "1.0.0", kind: "mode", mode: "default", user_id: "u2" },
          promptRelayOp(1, "web-other", SAID).payload.events[0],
        ],
      },
    });

    expect(modes).toEqual(["default"]);
    expect(userTexts(await source.getChatTurns(CHAT))).toEqual([SAID]);
  });
});
