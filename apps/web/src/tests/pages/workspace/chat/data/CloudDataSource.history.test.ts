// Reopening a chat must show the chat, not the last few seconds of it.
//
// The document's snapshot is the RETAINED WINDOW, not the transcript: it is
// whatever the realtime doc still holds. The durable record is REST. Gating the
// history read on "have we folded anything for this chat" let the snapshot
// satisfy the gate, so whenever the socket won the race the reader opened a
// chat of hundreds of events on a handful of them — sometimes on the empty
// state. The gate is the REST cursor, which only a durable read writes.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";

const NOW = "2026-09-06T12:00:00Z";

function textEvents(messageId: string, text: string): Record<string, unknown>[] {
  return [
    { event_type: "message.created", message_id: messageId, role: "assistant" },
    {
      event_type: "part.created",
      message_id: messageId,
      part: { part_id: `${messageId}-t`, message_id: messageId, type: "text", text },
    },
  ];
}

function docEntry(eventId: string, payload: Record<string, unknown>) {
  return { event_id: eventId, role: "assistant", kind: String(payload.event_type), payload };
}

/** A REST row as the SERVER serves it: docsync persists the whole published
 *  ENTRY as the row's payload, so the harness event is one level in. Recorded
 *  from a real run in
 *  `packages/api-core/tests/fixtures/objects/seam/chat_messages_page_from_server.json`
 *  and read there by `CloudDataSource.serverPage.test.ts`. */
function message(seq: number, eventId: string, payload: Record<string, unknown>) {
  return {
    id: `m-${seq}`,
    chat_id: "c1",
    seq,
    role: "assistant" as const,
    kind: String(payload.event_type),
    event_id: eventId,
    payload: docEntry(eventId, payload),
    created_at: NOW,
  };
}

function fakeDoc() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  const handle: DocHandle<unknown> = {
    onMessage: (listener) => {
      listeners.add(listener);
      return () => void listeners.delete(listener);
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
  };
  return {
    handle,
    snapshot(events: unknown[]): void {
      listeners.forEach((l) => l({ kind: "snapshot", state: { events }, epoch: 1, seq: 0 }));
    },
  };
}

/** The durable record, answered idempotently from a fixed history, and held
 *  behind a gate the test opens — so "did the source read REST at all?" is a
 *  fact the test decides rather than a race it hopes to win. */
function restOverHistory(history: ReturnType<typeof message>[]) {
  let open!: () => void;
  const gate = new Promise<void>((resolve) => {
    open = resolve;
  });
  const listMessages = vi.fn(async (_chatId: string, opts?: { afterSeq?: number }) => {
    await gate;
    const after = opts?.afterSeq ?? 0;
    const items = history.filter((m) => m.seq > after);
    return {
      items,
      next_after_seq: items.length > 0 ? items[items.length - 1].seq : after,
      resync_from: null,
    };
  });
  return { listMessages, open, rest: { listMessages } };
}

function transcriptText(turns: { parts: { kind: string }[] }[]): string[] {
  return turns.flatMap((turn) =>
    turn.parts
      .filter((p): p is { kind: string; text: string } => p.kind === "text" && "text" in p)
      .map((p) => p.text),
  );
}

/** The whole history in the durable record; only the tail still in the doc. */
function scene() {
  const history = [
    message(1, "e1", textEvents("a1", "the first answer")[0]),
    message(2, "e2", textEvents("a1", "the first answer")[1]),
    message(3, "e3", textEvents("a2", "the middle answer")[0]),
    message(4, "e4", textEvents("a2", "the middle answer")[1]),
    message(5, "e5", textEvents("a3", "the newest answer")[0]),
    message(6, "e6", textEvents("a3", "the newest answer")[1]),
  ];
  const doc = fakeDoc();
  const { rest, listMessages, open } = restOverHistory(history);
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  return { source, doc, listMessages, open };
}

/** The retained window: only the newest message pair is still in the doc. */
const WINDOW = [
  docEntry("e5", textEvents("a3", "the newest answer")[0]),
  docEntry("e6", textEvents("a3", "the newest answer")[1]),
];

describe("opening a chat the socket answered first", () => {
  it("still pages the durable history before handing over the transcript", async () => {
    const { source, doc, open } = scene();
    source.subscribeChat("c1", () => undefined);
    doc.snapshot(WINDOW);

    // The reader asks for the transcript while REST has not answered yet, so
    // the snapshot arrives first.
    const turns = source.getChatTurns("c1");
    open();

    // Every durable turn is there. (Their ORDER when the window is folded before
    // the durable read is the snapshot/replay path's own concern, not this
    // gate's: the reader's own open reads REST first, so the window dedupes into
    // an already-ordered fold.)
    expect(transcriptText(await turns).sort()).toEqual(
      ["the first answer", "the middle answer", "the newest answer"].sort(),
    );
  });

  it("pages the history for a chat the socket has said nothing about", async () => {
    const { source, open } = scene();
    open();
    expect(transcriptText(await source.getChatTurns("c1"))).toEqual([
      "the first answer",
      "the middle answer",
      "the newest answer",
    ]);
  });

  it("does not re-page REST on every look once the chat has been read", async () => {
    const { source, listMessages, open } = scene();
    open();
    await source.getChatTurns("c1");
    const afterFirst = listMessages.mock.calls.length;
    await source.getChatTurns("c1");
    await source.getChatTurns("c1");
    expect(listMessages.mock.calls.length).toBe(afterFirst);
  });

  it("reads REST once for an empty chat, not on every look", async () => {
    const doc = fakeDoc();
    const { rest, listMessages, open } = restOverHistory([]);
    const source = new CloudDataSource({
      rest: rest as never,
      openDoc: () => doc.handle as DocHandle<never>,
      acquire: () => () => undefined,
      clientId: () => "client-1",
    });
    open();

    expect(await source.getChatTurns("c1")).toEqual([]);
    const afterFirst = listMessages.mock.calls.length;
    await source.getChatTurns("c1");
    expect(listMessages.mock.calls.length).toBe(afterFirst);
  });
});
