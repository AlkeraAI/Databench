// A message a reader sent survives a reload before anything has answered it.
//
// The optimistic bubble the store pushes on Send lives in memory; the durable
// thing is the `prompt` row the server writes the moment it accepts the
// message. That row is not a harness event — it names no `event_type` — so the
// fold dropped it, and a reader who reloaded while the box was still starting
// (or never started) opened a chat with their own question missing from it.
//
// The contract here: the transcript carries the prompt from the moment the
// server took it, in the reader's own words, reading as still going out until a
// machine picks the turn up — and the box's later echo of the same words
// settles that turn rather than saying it twice.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle } from "@/api/realtime/docSync";

const NOW = "2026-09-16T12:00:00Z";
const CHAT = "c1";

function idleDoc(): DocHandle<never> {
  return {
    onMessage: () => () => undefined,
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

/** A row the MACHINE published: the envelope, with the event one level in. */
function machineRow(seq: number, role: "user" | "assistant", payload: Record<string, unknown>) {
  const eventId = String(payload.event_id);
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role,
    kind: String(payload.event_type),
    event_id: eventId,
    payload: { event_id: eventId, role, kind: String(payload.event_type), payload },
    created_at: NOW,
  };
}

/** The two events a harness publishes when it accepts the person's message. */
function echoOf(text: string, messageId = "hm1") {
  return [
    machineRow(10, "user", {
      event_id: `${messageId}-created`,
      event_type: "message.created",
      message_id: messageId,
      role: "user",
    }),
    machineRow(11, "user", {
      event_id: `${messageId}-part`,
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    }),
  ];
}

function sourceOver(pages: Record<string, unknown>[][]) {
  const listMessages = vi.fn();
  for (const items of pages) {
    listMessages.mockResolvedValueOnce({ items, next_after_seq: 999, resync_from: null });
  }
  listMessages.mockResolvedValue({ items: [], next_after_seq: 999, resync_from: null });
  const postMessage = vi.fn();
  return {
    listMessages,
    postMessage,
    source: new CloudDataSource({
      rest: { listMessages, postMessage } as never,
      openDoc: () => idleDoc(),
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

describe("a message that has been sent but not yet taken", () => {
  it("stands in the transcript the moment the send returns, still going out", async () => {
    const { source, postMessage } = sourceOver([[promptRow(1, "web-1", "what is 6*7?")]]);
    postMessage.mockResolvedValue(promptRow(1, "web-1", "what is 6*7?"));

    await source.sendUserMessage(CHAT, "what is 6*7?");
    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual(["what is 6*7?"]);
    expect(turns[0]?.status, "a message nothing has taken reads as still going out").toBe(
      "running",
    );
  });

  it("is on the page a RELOAD reads — a fresh source that never saw the send", async () => {
    // Exactly what a browser does on refresh: a new source, no memory of the
    // optimistic bubble, reading the server's own page.
    const { source } = sourceOver([[promptRow(1, "web-1", "what is 6*7?")]]);

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual(["what is 6*7?"]);
    expect(turns[0]?.status).toBe("running");
  });

  it("stays visible when no box ever takes it — a later read does not retire it", async () => {
    const { source } = sourceOver([
      [promptRow(1, "web-1", "what is 6*7?")],
      [],
      [],
    ]);

    await source.getChatTurns(CHAT);
    await source.reopenChat(CHAT);
    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual(["what is 6*7?"]);
    expect(turns[0]?.status).toBe("running");
  });

  it("is said ONCE once the box echoes it back, and stops reading as going out", async () => {
    const { source } = sourceOver([
      [promptRow(1, "web-1", "what is 6*7?"), ...echoOf("what is 6*7?")],
    ]);

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns), "the echo must not say the message a second time").toEqual([
      "what is 6*7?",
    ]);
    expect(turns[0]?.status).toBe("done");
  });

  it("is said once even when the echo carries the attachment block the box added", async () => {
    // The box hands the harness the files above the words, so its echo is not
    // character-for-character the message. The words are the tail of it, and
    // the words are what the reader wrote.
    const said = "compare these";
    const heard = `The reader attached these files:\n- /w/orders.csv (12 bytes)\n\n${said}`;
    const { source } = sourceOver([[promptRow(1, "web-1", said), ...echoOf(heard)]]);

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual([said]);
  });

  it("stops reading as going out once the machine answers without echoing it", async () => {
    const { source } = sourceOver([
      [
        promptRow(1, "web-1", "what is 6*7?"),
        machineRow(2, "assistant", {
          event_id: "a1-created",
          event_type: "message.created",
          message_id: "a1",
          role: "assistant",
        }),
        machineRow(3, "assistant", {
          event_id: "a1-part",
          event_type: "part.created",
          message_id: "a1",
          part: { part_id: "a1-t", message_id: "a1", type: "text", text: "Answer: 42" },
        }),
      ],
    ]);

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual(["what is 6*7?"]);
    expect(turns[0]?.status, "the machine is speaking, so the message was taken").toBe("done");
    expect(JSON.stringify(turns)).toContain("Answer: 42");
  });

  it("carries the words and never the hidden context recorded beside them", async () => {
    const row = promptRow(1, "web-1", "what changed?");
    row.payload.context = "<slack-channel-history>secret briefing</slack-channel-history>";
    const { source } = sourceOver([[row]]);

    const turns = await source.getChatTurns(CHAT);

    expect(userTexts(turns)).toEqual(["what changed?"]);
    expect(JSON.stringify(turns)).not.toContain("secret briefing");
  });
});
