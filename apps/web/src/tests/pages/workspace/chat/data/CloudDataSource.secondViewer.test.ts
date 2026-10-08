// Two people reading the same chat see the same chat.
//
// A second person opens a chat that is already long and already answering.
// Their browser gets the document's RETAINED WINDOW (the last few events, not
// the transcript), and everything older has to come from the durable record.
// This pins the invariant from both sides: whatever order
// the socket and REST answer in, the second viewer's transcript is the first
// viewer's transcript, and the live turn arriving reaches them both.

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

type Message = ReturnType<typeof message>;

/** One document, as many readers as the test opens. Each `openDoc` call is its
 *  own handle, exactly as a second browser tab gets its own. */
function fanoutDoc() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  const open = (): DocHandle<unknown> => {
    const mine = new Set<(m: DocMessage<unknown>) => void>();
    return {
      onMessage: (listener) => {
        listeners.add(listener);
        mine.add(listener);
        return () => {
          listeners.delete(listener);
          mine.delete(listener);
        };
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
      dispose: () => mine.forEach((l) => listeners.delete(l)),
    };
  };
  return {
    open,
    /** What a joining reader is handed: the retained window, nothing older —
     *  plus whatever the publisher last wrote on the meta lane. */
    snapshot(events: unknown[], meta?: Record<string, unknown>): void {
      listeners.forEach((l) => l({ kind: "snapshot", state: { events, meta }, epoch: 1, seq: 0 }));
    },
    /** A meta write reaching every open reader, as the server's own peer. */
    meta(meta: Record<string, unknown>): void {
      listeners.forEach((l) =>
        l({
          kind: "op",
          payload: { op_id: "m1", intent: "set_meta", meta },
          peerId: "server",
          epoch: 1,
          seq: 2,
          ephemeral: false,
        }),
      );
    },
    /** A durable append reaching every open reader. */
    append(events: unknown[]): void {
      listeners.forEach((l) =>
        l({
          kind: "op",
          payload: { op_id: "o1", intent: "append", events },
          peerId: "p:2",
          epoch: 1,
          seq: 1,
          ephemeral: false,
        }),
      );
    },
  };
}

/** The durable record. Every viewer reads the same one, and each read is
 *  counted so "the second viewer never asked" is visible rather than inferred. */
function rest(history: Message[]) {
  const listMessages = vi.fn(async (_chatId: string, opts?: { afterSeq?: number }) => {
    const after = opts?.afterSeq ?? 0;
    const items = history.filter((m) => m.seq > after);
    return {
      items,
      next_after_seq: items.length > 0 ? items[items.length - 1].seq : after,
      resync_from: null,
    };
  });
  return { listMessages, rest: { listMessages } };
}

function transcriptText(turns: { parts: { kind: string }[] }[]): string[] {
  return turns.flatMap((turn) =>
    turn.parts
      .filter((p): p is { kind: string; text: string } => p.kind === "text" && "text" in p)
      .map((p) => p.text),
  );
}

/** A chat with real history behind a short retained window — the shape the
 *  two-viewer beat actually meets. */
function scene() {
  const history: Message[] = [];
  const said: string[] = [];
  for (let i = 1; i <= 5; i += 1) {
    const text = `answer ${i}`;
    said.push(text);
    const [created, part] = textEvents(`a${i}`, text);
    history.push(message(history.length + 1, `e${history.length + 1}`, created));
    history.push(message(history.length + 1, `e${history.length + 1}`, part));
  }
  const doc = fanoutDoc();
  const { rest: transport, listMessages } = rest(history);
  const viewer = () =>
    new CloudDataSource({
      rest: transport as never,
      openDoc: () => doc.open() as DocHandle<never>,
      acquire: () => () => undefined,
      clientId: () => "client",
    });
  /** Only the newest exchange is still in the document. */
  const window = history.slice(-2).map((m) => m.payload);
  return { viewer, doc, window, said, listMessages };
}

describe("a second viewer joining a chat that is already long", () => {
  it("sees everything the first viewer sees, not just the retained window", async () => {
    const { viewer, doc, window, said } = scene();

    const first = viewer();
    first.subscribeChat("c1", () => undefined);
    doc.snapshot(window);
    const firstTurns = transcriptText(await first.getChatTurns("c1"));

    // The second tab opens now: its own handle, its own hello, the same
    // retained window — and four fifths of the conversation missing from it.
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot(window);
    const secondTurns = transcriptText(await second.getChatTurns("c1"));

    expect(firstTurns.sort()).toEqual([...said].sort());
    expect(secondTurns.sort()).toEqual(firstTurns.sort());
  });

  it("still has the whole chat when its snapshot lands before it asks", async () => {
    // The other order: the socket answers this reader before anything asks for
    // the transcript, so the snapshot's own catch-up is what fills the history.
    const { viewer, doc, window, said } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot(window);
    // Let the snapshot's own durable read settle before the reader looks.
    await Promise.resolve();
    await Promise.resolve();

    expect(transcriptText(await second.getChatTurns("c1")).sort()).toEqual([...said].sort());
  });

  it("takes the live turn as it streams, on top of the history it just read", async () => {
    const { viewer, doc, window, said } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot(window);
    await second.getChatTurns("c1");

    const [created, part] = textEvents("a6", "the answer they watched arrive");
    doc.append([docEntry("e11", created), docEntry("e12", part)]);

    expect(transcriptText(await second.getChatTurns("c1")).sort()).toEqual(
      [...said, "the answer they watched arrive"].sort(),
    );
  });
});

/** The live turn as the retained window holds it mid-flight: the assistant
 *  message is open and its `sql.query` is still running. */
function runningQuery(): unknown[] {
  return [
    docEntry("e11", { event_type: "message.created", message_id: "a6", role: "assistant" }),
    docEntry("e12", {
      event_type: "tool.call",
      message_id: "a6",
      tool_call_id: "call-sql",
      tool_name: "sql_query",
      status: "running",
    }),
  ];
}

/** The publisher's last word on the turn, stamped `ageMs` ago. */
function working(ageMs = 0): Record<string, unknown> {
  const at = new Date(Date.now() - ageMs).toISOString();
  return { turn_state: { state: "working", at }, turn_state_at: at };
}

function toolStates(turns: { parts: { kind: string }[] }[]): string[] {
  return turns.flatMap((turn) =>
    turn.parts
      .filter((p): p is { kind: string; state: string } => p.kind === "tool" && "state" in p)
      .map((p) => p.state),
  );
}

describe("a second viewer joining while the turn is still running", () => {
  it("leaves the in-flight query running when the machine says it is working", async () => {
    // Ask on the member's tab, then open the admin's
    // while the query is still on the wire. The joining tab's snapshot IS a
    // replay, but the same frame says `working` — nothing in it is stale, and a
    // card reading "1 failed" for the next 60 s is two viewers seeing two
    // different chats.
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], working());

    const turns = await second.getChatTurns("c1");
    expect(toolStates(turns)).toEqual(["running"]);
    expect(turns.map((turn) => turn.status)).not.toContain("cancelled");
  });

  it("keeps the watched turn alive through the result that finishes it", async () => {
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], working());
    await second.getChatTurns("c1");

    doc.append([
      docEntry("e13", {
        event_type: "tool.call_update",
        message_id: "a6",
        tool_call_id: "call-sql",
        status: "completed",
      }),
      docEntry("e14", { event_type: "turn.finished", turn_id: "t6", stop_reason: "end_turn" }),
    ]);

    const turns = await second.getChatTurns("c1");
    expect(toolStates(turns)).toEqual(["completed"]);
    // The later update repairs the tool's own state either way; what a settled
    // replay cannot take back is the turn it cancelled underneath it, which
    // reads as a failed exchange for the rest of the conversation.
    const ran = turns.find((turn) => turn.parts.some((part) => part.kind === "tool"));
    expect(ran?.status).not.toBe("cancelled");
  });

  it("still settles a cold open the machine calls idle", async () => {
    // The guard is the machine's word, not a blanket amnesty: a transcript
    // whose publisher stopped mid-tool still has to read as failed rather than
    // spin forever.
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], { turn_state: { state: "idle" } });

    expect(toolStates(await second.getChatTurns("c1"))).toEqual(["error"]);
  });

  it("keeps believing a `working` written hours ago while the box is still there", async () => {
    // A turn may legitimately run for hours — one warehouse query, one
    // compaction, one subagent — and the publisher stamps `working` once, at the
    // start. The age of that stamp says nothing about whether the turn ended, so
    // it decides nothing: a five-minute bound here read a three-hour query as a
    // crash to everyone who opened the chat after minute five.
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], working(6 * 60 * 60_000));

    expect(toolStates(await second.getChatTurns("c1"))).toEqual(["running"]);
  });

  it("takes an unstamped `working` on trust while the box is still there", async () => {
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], { turn_state: { state: "working" } });

    expect(toolStates(await second.getChatTurns("c1"))).toEqual(["running"]);
  });

  it("keeps the turn running whatever the compute plane says, until the document says idle", async () => {
    // The compute plane is a banner, never a verdict on the turn: a box that
    // missed its heartbeats comes back holding the turn, and a box that is gone
    // has its turn ended by the server, which says so on the document.
    const { viewer, doc, window } = scene();
    const second = viewer();
    second.subscribeChat("c1", () => undefined);
    doc.snapshot([...window, ...runningQuery()], working());
    expect(toolStates(await second.getChatTurns("c1"))).toEqual(["running"]);

    // The server ended the turn its box will never finish.
    doc.meta({ turn_state: { state: "idle" } });

    const turns = await second.getChatTurns("c1");
    expect(toolStates(turns)).toEqual(["error"]);
    expect(second.turnState("c1")).toBe("idle");
  });
});
