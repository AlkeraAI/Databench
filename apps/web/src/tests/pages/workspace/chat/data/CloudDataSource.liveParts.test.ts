// A thought the box streams is shown once, not once per path that carries it.
//
// The live document delivers a reasoning part three times over: the per-token
// deltas on the ephemeral lane, the finalized part on the durable lane, and the
// same finalized part again on the REST page a reader (re)fetches. Each
// telling names the same part, so the fold must land them on one part — a
// second copy is the reader watching the model think twice.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle } from "@/api/realtime/docSync";

const NOW = "2026-09-16T12:00:00Z";
const CHAT = "c1";
const THOUGHT = "Let me look at the orders table first.";

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
  return { handle, push: (message) => listeners.forEach((listener) => listener(message)) };
}

function entryOf(role: "assistant", event: Record<string, unknown>) {
  return {
    event_id: String(event.event_id),
    role,
    kind: String(event.event_type),
    payload: event,
  };
}

function machineRow(seq: number, event: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: "assistant" as const,
    kind: String(event.event_type),
    event_id: String(event.event_id),
    payload: entryOf("assistant", event),
    created_at: NOW,
  };
}

const MESSAGE_CREATED = {
  event_id: "a1-created",
  event_type: "message.created",
  message_id: "a1",
  role: "assistant",
};

/** The part opens empty — opencode announces the part before any token. */
const PART_STARTED = {
  event_id: "a1-p1-started",
  event_type: "part.started",
  message_id: "a1",
  part_id: "p1",
  part_type: "reasoning",
  initial: { type: "reasoning", text: "" },
};

/** The per-token deltas, on the ephemeral lane (never persisted). */
function thoughtChunks(text: string): Record<string, unknown>[] {
  return text.split(" ").map((word, i, all) => ({
    event_id: `a1-p1-chunk-${i}`,
    event_type: "agent.thought_chunk",
    message_id: "a1",
    part_id: "p1",
    sequence: i,
    text: i === 0 ? word : ` ${word}`,
    is_final: i === all.length - 1,
  }));
}

/** The finalized part, on the durable lane. */
const PART_CREATED = {
  event_id: "a1-p1-created",
  event_type: "part.created",
  message_id: "a1",
  part: { part_id: "p1", message_id: "a1", type: "reasoning", text: THOUGHT },
};

function appendOp(events: Record<string, unknown>[], ephemeral = false) {
  return {
    kind: "op",
    ephemeral,
    payload: {
      op_id: `op-${String(events[0]?.event_id)}`,
      intent: ephemeral ? "chunk" : "append",
      events: events.map((event) => entryOf("assistant", event)),
    },
  };
}

function sourceOver(pages: Record<string, unknown>[][]) {
  const listMessages = vi.fn();
  for (const items of pages) {
    listMessages.mockResolvedValueOnce({ items, next_after_seq: 999, resync_from: null });
  }
  listMessages.mockResolvedValue({ items: [], next_after_seq: 999, resync_from: null });
  const doc = scriptedDoc();
  return {
    push: doc.push,
    source: new CloudDataSource({
      rest: { listMessages, postMessage: vi.fn() } as never,
      openDoc: () => doc.handle,
      acquire: () => () => undefined,
      clientId: () => "web-1",
    }),
  };
}

function thoughts(turns: { parts: { kind: string; text?: string }[] }[]): string[] {
  return turns.flatMap((turn) =>
    turn.parts.filter((part) => part.kind === "thinking").map((part) => part.text ?? ""),
  );
}

/** The durable page a reader reads back: everything but the deltas. */
const DURABLE_PAGE = [
  machineRow(1, MESSAGE_CREATED),
  machineRow(2, PART_STARTED),
  machineRow(3, PART_CREATED),
];

describe("a thought the box is thinking out loud", () => {
  it("is one part when the deltas stream and the finalized part follows", async () => {
    const { source, push } = sourceOver([DURABLE_PAGE]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp([MESSAGE_CREATED, PART_STARTED]));
    push(appendOp(thoughtChunks(THOUGHT), true));
    push(appendOp([PART_CREATED]));

    const turns = await source.getChatTurns(CHAT);
    expect(thoughts(turns), "the reader watches the model think once").toEqual([THOUGHT]);
  });

  it("is one part when the finalized part lands before the deltas it replaces", async () => {
    // The ephemeral lane has no ordering guarantee against the durable one: a
    // delta can arrive after the part it belongs to was already settled.
    const { source, push } = sourceOver([DURABLE_PAGE]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp([MESSAGE_CREATED, PART_STARTED]));
    push(appendOp([PART_CREATED]));
    push(appendOp(thoughtChunks(THOUGHT), true));

    const turns = await source.getChatTurns(CHAT);
    expect(thoughts(turns)).toEqual([THOUGHT]);
  });

  it("is one part when the durable page is read while the deltas are still streaming", async () => {
    const { source, push } = sourceOver([DURABLE_PAGE]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp([MESSAGE_CREATED, PART_STARTED]));
    push(appendOp(thoughtChunks(THOUGHT), true));
    // The page read that a reconnect (or the first open) runs mid-turn.
    await source.getChatTurns(CHAT);
    push(appendOp([PART_CREATED]));

    const turns = await source.getChatTurns(CHAT);
    expect(thoughts(turns)).toEqual([THOUGHT]);
  });

  it("keeps a second thought in the same message as its own part", async () => {
    // The guard on the fix: two parts are two thoughts, not a duplicate.
    const second = {
      event_id: "a1-p2-created",
      event_type: "part.created",
      message_id: "a1",
      part: { part_id: "p2", message_id: "a1", type: "reasoning", text: "Then the join." },
    };
    const { source, push } = sourceOver([[...DURABLE_PAGE, machineRow(4, second)]]);
    source.subscribeChat(CHAT, () => undefined);

    push(appendOp([MESSAGE_CREATED, PART_STARTED]));
    push(appendOp([PART_CREATED]));
    push(appendOp([second]));

    const turns = await source.getChatTurns(CHAT);
    expect(thoughts(turns)).toEqual([THOUGHT, "Then the join."]);
  });
});
