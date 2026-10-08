// The browser's chat source against its two transports.
//
// Everything here is about the seam between a document and a transcript: what
// the snapshot seeds, what an append adds, what a token chunk does that an
// append does not, and — the case that decides whether a reader ever sees a
// wrong transcript — what happens when the same entry arrives twice, or when
// the reader's cursor has fallen out of the server's retained window.

import { beforeEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { queryClient } from "@/api/queryClient";
import { APPROVAL_PENDING } from "@/pages/workspace/chat/data/ChatDataSource";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { pendingAskKind } from "@/pages/workspace/chat/data/harnessEventFold";
import type { DocHandle, DocMessage, OpPayload } from "@/api/realtime/docSync";

const NOW = "2026-09-06T12:00:00Z";

/** The two harness events that make one assistant text turn. */
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

/** A transcript entry as it rides the chat document. */
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
  let disposed = false;
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
    dispose: () => {
      disposed = true;
    },
  };
  return {
    handle,
    wasDisposed: () => disposed,
    snapshot(events: unknown[]): void {
      listeners.forEach((l) => l({ kind: "snapshot", state: { events }, epoch: 1, seq: 0 }));
    },
    op(payload: OpPayload, opts: { ephemeral?: boolean; seq?: number } = {}): void {
      listeners.forEach((l) =>
        l({
          kind: "op",
          payload,
          peerId: "pub:1",
          epoch: 1,
          seq: opts.seq ?? 1,
          ephemeral: opts.ephemeral ?? false,
        }),
      );
    },
  };
}

/** What the server says a card offers on a write in each stance: its verdict,
 *  written out here as the stub server's own table. The server's table is
 *  pinned to the box's stance rows in `test_stance_contract.py`. */
const SERVER_VERDICT: Record<string, string | null> = {
  read_only: "This workspace is read-only, so it won't run this.",
  plan: "Plan mode explores and proposes a plan, so it won't run this.",
  default: null,
  auto: null,
  bypass: null,
};

function restStub(pages: { items: ReturnType<typeof message>[]; next_after_seq: number; resync_from: number | null }[]) {
  const queue = [...pages];
  const listMessages = vi.fn(async () => queue.shift() ?? { items: [], next_after_seq: 0, resync_from: null });
  const posted: { text: string; clientId: string }[] = [];
  const answerInterrupt = vi.fn(async () => undefined);
  // The chat as the SERVER serves it, and the store the mode routes write to:
  // `PUT /permission-mode` answers with the row it stored, which is what the
  // source is supposed to believe rather than its own optimistic guess.
  const stored = { permission_mode: "read_only" as string };
  const getChat = vi.fn(async () => ({
    id: "c1",
    title: "Chat",
    machine_id: null,
    machine_status: "none" as const,
    created_at: NOW,
    updated_at: NOW,
    last_seq: 0,
    model: null,
    permission_mode: stored.permission_mode,
    approval_refusal: SERVER_VERDICT[stored.permission_mode] ?? null,
  }));
  const setPermissionMode = vi.fn(async (_chatId: string, mode: string) => {
    stored.permission_mode = mode;
    return { ...(await getChat()), permission_mode: mode };
  });
  const chatModels = vi.fn(async () => ({
    items: [
      {
        id: "model-alpha",
        display_name: "Model Alpha",
        wire: "anthropic" as const,
        efforts: ["low", "high"],
        default_effort: "high",
      },
    ],
  }));
  const chatDefaults = vi.fn(async () => ({
    model: "model-alpha",
    effort: "high",
    permission_mode: "plan",
  }));
  return {
    listMessages,
    posted,
    answerInterrupt,
    getChat,
    setPermissionMode,
    chatModels,
    chatDefaults,
    stored,
    rest: {
      listChats: vi.fn(async () => ({ items: [], next_cursor: null })),
      createChat: vi.fn(async (title: string | null, opts: Record<string, unknown> = {}) => ({
        id: "created",
        title: title ?? "Untitled chat",
        machine_id: null,
        machine_status: "none" as const,
        created_at: NOW,
        updated_at: NOW,
        last_seq: 0,
        model: opts.model
          ? {
              id: String(opts.model),
              display_name: "Model Alpha",
              wire: "anthropic",
              efforts: ["low", "high"],
              effort: opts.effort ?? null,
              context_window: 0,
              max_output_tokens: 0,
            }
          : null,
        permission_mode: "read_only" as const,
      })),
      getChat,
      setPermissionMode,
      chatModels,
      chatDefaults,
      listMessages,
      answerInterrupt,
      postMessage: vi.fn(async (_chatId: string, body: { text: string; client_id: string }) => {
        posted.push({ text: body.text, clientId: body.client_id });
        return message(99, "sent-1", {
          event_type: "message.created",
          message_id: "u-1",
          role: "user",
        });
      }),
    },
  };
}

function build(pages: Parameters<typeof restStub>[0] = []) {
  const doc = fakeDoc();
  const stub = restStub(pages);
  let released = 0;
  const source = new CloudDataSource({
    rest: stub.rest as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => {
      released += 1;
    },
    clientId: () => "client-1",
  });
  return { ...stub, source, doc, releases: () => released };
}

/** The plain text of every assistant turn, in order. */
function transcriptText(turns: { parts: { kind: string }[] }[]): string[] {
  return turns.flatMap((turn) =>
    turn.parts
      .filter((p): p is { kind: string; text: string } => p.kind === "text" && "text" in p)
      .map((p) => p.text),
  );
}

describe("CloudDataSource transcript", () => {
  let subject: ReturnType<typeof build>;

  beforeEach(() => {
    subject = build();
  });

  it("seeds the transcript from the document's snapshot", async () => {
    const [created, part] = textEvents("a1", "the snapshot line");
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([docEntry("e1", created), docEntry("e2", part)]);

    const turns = await subject.source.getChatTurns("c1");
    expect(transcriptText(turns)).toEqual(["the snapshot line"]);
  });

  it("adds an appended entry to the transcript and tells its subscribers", async () => {
    const seen: string[] = [];
    subject.source.subscribeChat("c1", (event) => seen.push(event.id));
    subject.doc.snapshot([]);
    const [created, part] = textEvents("a2", "appended after the snapshot");
    subject.doc.op({ op_id: "o1", intent: "append", events: [docEntry("e3", created), docEntry("e4", part)] });

    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual([
      "appended after the snapshot",
    ]);
    expect(seen.length).toBeGreaterThan(0);
  });

  it("folds a token chunk from the ephemeral lane into the assistant's turn", async () => {
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([docEntry("e5", textEvents("a3", "")[0])]);
    subject.doc.op(
      {
        op_id: "o2",
        intent: "chunk",
        events: [
          {
            event_type: "agent.message_chunk",
            message_id: "a3",
            part_id: "a3-t",
            text: "streamed ",
          },
        ],
      },
      { ephemeral: true, seq: 0 },
    );
    subject.doc.op(
      {
        op_id: "o3",
        intent: "chunk",
        events: [
          { event_type: "agent.message_chunk", message_id: "a3", part_id: "a3-t", text: "token" },
        ],
      },
      { ephemeral: true, seq: 0 },
    );

    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["streamed token"]);
  });

  it("folds a durable entry exactly once, however many times it arrives", async () => {
    // The publisher may re-send after a reconnect, and a REST page overlaps the
    // window the snapshot already carried. Folding twice would double the text.
    const [created, part] = textEvents("a4", "said once");
    subject = build([{ items: [message(1, "e6", created), message(2, "e7", part)], next_after_seq: 2, resync_from: null }]);
    await subject.source.getChatTurns("c1");
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([docEntry("e6", created), docEntry("e7", part)]);
    subject.doc.op({ op_id: "o4", intent: "append", events: [docEntry("e7", part)] });

    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["said once"]);
  });

  it("ignores an op it does not understand rather than throwing on it", async () => {
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([]);
    subject.doc.op({ op_id: "o5", intent: "set_meta", meta: { title: "renamed" } });
    await expect(subject.source.getChatTurns("c1")).resolves.toEqual([]);
  });

  it("drops a document entry carrying no harness event", async () => {
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([{ event_id: "e8", role: "assistant", kind: "unknown" }, "not an object"]);
    await expect(subject.source.getChatTurns("c1")).resolves.toEqual([]);
  });
});

describe("CloudDataSource history and recovery", () => {
  it("reads history forward until the server has no more to give", async () => {
    const [created, part] = textEvents("a5", "from history");
    const subject = build([
      { items: [message(1, "h1", created)], next_after_seq: 1, resync_from: null },
      { items: [message(2, "h2", part)], next_after_seq: 2, resync_from: null },
      { items: [], next_after_seq: 2, resync_from: null },
    ]);
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["from history"]);
    expect(subject.listMessages).toHaveBeenCalledTimes(3);
  });

  it("a resync marker restarts the read AT the oldest message the server still holds", async () => {
    // The in-band reset: the reader asked for a sequence older than the retained
    // window, and is told where to start again rather than handed an error.
    // `resync_from` names a message that EXISTS ("start again from here"), and
    // `afterSeq` is exclusive — so resuming at the marker itself would drop the
    // oldest message the server has left, every time the reset path is taken.
    const [created, part] = textEvents("a6", "the oldest one left");
    const subject = build([
      { items: [], next_after_seq: 0, resync_from: 40 },
      { items: [message(40, "r0", created), message(41, "r1", part)], next_after_seq: 41, resync_from: null },
      { items: [], next_after_seq: 41, resync_from: null },
    ]);
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["the oldest one left"]);
    expect(subject.listMessages.mock.calls[1]).toEqual(["c1", { afterSeq: 39 }]);
  });

  it("the tightest resync the server can send still hands back the message it names", async () => {
    // The exact boundary: the reader is at 0 and only message 2 survives, which
    // is the smallest marker the server ever emits (it sends one only when the
    // first message asked for is older than the oldest held). Resuming at 2
    // would ask for everything after it and lose the one message left.
    const [created, part] = textEvents("a6b", "the last survivor");
    const subject = build([
      { items: [], next_after_seq: 0, resync_from: 2 },
      { items: [message(2, "b0", created), message(3, "b1", part)], next_after_seq: 3, resync_from: null },
      { items: [], next_after_seq: 3, resync_from: null },
    ]);
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["the last survivor"]);
    expect(subject.listMessages.mock.calls[1]).toEqual(["c1", { afterSeq: 1 }]);
  });

  it("a marker naming the very next message is not a reset, and never loops", async () => {
    // `resync_from` at `afterSeq + 1` says the cursor is already where the
    // history starts. Treating it as a reset would rewind to the same place and
    // ask again, forever.
    const [created, part] = textEvents("a6c", "nothing was lost");
    const subject = build([
      { items: [message(1, "n0", created), message(2, "n1", part)], next_after_seq: 2, resync_from: 1 },
      { items: [], next_after_seq: 2, resync_from: null },
    ]);
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["nothing was lost"]);
    expect(subject.listMessages).toHaveBeenCalledTimes(2);
  });

  it("a resync discards what was folded before it, rather than stacking on it", async () => {
    const [created, part] = textEvents("a7", "the only line");
    const subject = build([
      { items: [message(1, "s1", created), message(2, "s2", part)], next_after_seq: 2, resync_from: null },
      { items: [], next_after_seq: 2, resync_from: null },
      { items: [], next_after_seq: 0, resync_from: 40 },
      { items: [message(41, "s3", created), message(42, "s4", part)], next_after_seq: 42, resync_from: null },
      { items: [], next_after_seq: 42, resync_from: null },
    ]);
    await subject.source.getChatTurns("c1");
    await subject.source.reopenChat("c1");
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["the only line"]);
  });
});

describe("CloudDataSource sends", () => {
  it("posts the message with a client id and folds the server's answer", async () => {
    const subject = build();
    await subject.source.sendUserMessage("c1", "how many orders yesterday?");
    expect(subject.posted).toEqual([{ text: "how many orders yesterday?", clientId: "client-1" }]);
  });

  it("never writes to the document — the publisher is its only writer", async () => {
    const subject = build();
    subject.source.subscribeChat("c1", () => undefined);
    await subject.source.sendUserMessage("c1", "a question");
    await expect(subject.doc.handle.sendOp({ intent: "append" })).rejects.toThrow(/never writes/);
  });
});

describe("CloudDataSource subscriptions", () => {
  it("holds the socket while a chat is subscribed and lets go when it is not", () => {
    const subject = build();
    const off = subject.source.subscribeChat("c1", () => undefined);
    expect(subject.releases()).toBe(0);
    off();
    expect(subject.releases()).toBe(1);
    expect(subject.doc.wasDisposed()).toBe(true);
  });

  it("re-reads the transcript on every snapshot, so a socket gap leaves no hole", async () => {
    // A snapshot is what a reconnect delivers, and it carries only the RETAINED
    // window: an append made while the socket was down and since compacted out
    // of that window is simply absent, with nothing to say so. The reader would
    // see a transcript with a gap in it until they reloaded the page.
    const [created, part] = textEvents("gap-1", "what the window dropped");
    const subject = build([
      { items: [message(7, "gap-1", created), message(8, "gap-2", part)], next_after_seq: 8, resync_from: null },
      { items: [], next_after_seq: 8, resync_from: null },
    ]);
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([]);
    await vi.waitFor(() => expect(subject.listMessages).toHaveBeenCalled());
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual([
      "what the window dropped",
    ]);
  });

  it("folds an entry the snapshot and the REST page both carry exactly once", async () => {
    // The two sources overlap by design — the window holds what REST also has —
    // so the merge has to dedupe by event id or every reconnect doubles the
    // recent transcript.
    const [created, part] = textEvents("dup-1", "said once");
    const subject = build([
      { items: [message(3, "dup-1", created), message(4, "dup-2", part)], next_after_seq: 4, resync_from: null },
      { items: [], next_after_seq: 4, resync_from: null },
    ]);
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([
      { event_id: "dup-1", payload: created },
      { event_id: "dup-2", payload: part },
    ]);
    await vi.waitFor(() => expect(subject.listMessages).toHaveBeenCalled());
    expect(transcriptText(await subject.source.getChatTurns("c1"))).toEqual(["said once"]);
  });

  it("opens one document however many readers a chat has", () => {
    const subject = build();
    const a = subject.source.subscribeChat("c1", () => undefined);
    const b = subject.source.subscribeChat("c1", () => undefined);
    expect(subject.releases()).toBe(0);
    a();
    // The last reader out closes it, not the first.
    expect(subject.doc.wasDisposed()).toBe(false);
    b();
    expect(subject.doc.wasDisposed()).toBe(true);
  });
});

describe("CloudDataSource capabilities", () => {
  it("says it drives no OpenCode harness, so the harness affordances stand down", () => {
    expect(new CloudDataSource().caps.opencodeActive).toBe(false);
  });

  // The catalogue and the harness stopped being the same fact: the browser
  // picks a model through the portal's own route and still has no harness to
  // ask for a slash-command registry.
  it("serves a model catalogue even though it drives no harness", () => {
    expect(new CloudDataSource().caps.modelCatalog).toBe(true);
  });

  // A reader who knows the extension's stances and does not find one of them in
  // the browser reads its absence as a product that cannot do it. The order is
  // the editor's too, so the same picker reads the same way in both shells.
  it("offers the editor's five stances, in the editor's order", () => {
    expect([...(new CloudDataSource().caps.permissionModes ?? [])]).toEqual([
      "default",
      "plan",
      "auto",
      "read_only",
      "bypass",
    ]);
  });

  it("listCommands answers emptily rather than failing", async () => {
    expect(await new CloudDataSource().listCommands()).toEqual([]);
  });

  it("hands back the gateway catalogue the portal route serves", async () => {
    const subject = build();
    expect(await subject.source.listModels()).toEqual([
      {
        id: "model-alpha",
        displayName: "Model Alpha",
        wire: "anthropic",
        efforts: ["low", "high"],
        defaultEffort: "high",
      },
    ]);
  });

  // The rules that decide a new chat's seed — above all "a gateway outage must
  // not wipe a saved default" — live on the server, resolved once for every
  // client. This source must not re-derive them, only carry the answer.
  it("carries the server's resolved new-chat defaults without re-deriving them", async () => {
    const subject = build();
    expect(await subject.source.resolveChatDefaults()).toEqual({
      model: "model-alpha",
      effort: "high",
      // Including the stance a new chat would open in: the composer's chip has
      // no chat to read it off yet, and the server is what decides it.
      permissionMode: "plan",
    });
    expect(subject.chatDefaults).toHaveBeenCalledTimes(1);
  });

  // The chat row is the authority — the box reads the same field to open the
  // session — so the mode is READ, never assumed from the source's floor.
  it("reads a chat's stance off the chat row", async () => {
    const subject = build();
    subject.stored.permission_mode = "plan";
    expect(await subject.source.getPermissionMode("c1")).toBe("plan");
  });

  it("puts the chat in a stance through the route and believes what it stored", async () => {
    const subject = build();
    await subject.source.setPermissionMode("c1", "default");
    expect(subject.setPermissionMode).toHaveBeenCalledWith("c1", "default");
    expect(await subject.source.getPermissionMode("c1")).toBe("default");
  });

  it("tells a mode subscriber about a switch, and stops when released", async () => {
    const subject = build();
    const seen: string[] = [];
    const off = subject.source.subscribePermissionMode("c1", (mode) => seen.push(mode));
    await subject.source.setPermissionMode("c1", "plan");
    expect(seen).toEqual(["plan"]);
    off();
    await subject.source.setPermissionMode("c1", "default");
    expect(seen).toEqual(["plan"]);
  });

  // A mode the BOX flipped (a plan approval accepted on the machine) reaches the
  // pill on the document's meta lane, the same one `turn_state` rides — without
  // it the chip would keep stating a stance the session left.
  it("follows a stance the publisher republishes on the document", async () => {
    const subject = build();
    const seen: string[] = [];
    subject.source.subscribePermissionMode("c1", (mode) => seen.push(mode));
    subject.source.subscribeChat("c1", () => {});
    subject.doc.op({
      op_id: "srv-1",
      intent: "set_meta",
      meta: { permission_mode: "default" },
    } as never);
    expect(seen).toEqual(["default"]);
  });

  // The meta is merged server-side, so a frame about something else must not
  // read as "the stance changed".
  it("leaves the stance standing when a frame says nothing about it", async () => {
    const subject = build();
    await subject.source.setPermissionMode("c1", "plan");
    const seen: string[] = [];
    subject.source.subscribePermissionMode("c1", (mode) => seen.push(mode));
    subject.source.subscribeChat("c1", () => {});
    subject.doc.op({
      op_id: "srv-2",
      intent: "set_meta",
      meta: { turn_state: { state: "working" } },
    } as never);
    expect(seen).toEqual([]);
    expect(await subject.source.getPermissionMode("c1")).toBe("plan");
  });

  // The model is chosen before the chat exists and pinned on the row the box
  // reads, so it has to ride the CREATE and not the first message.
  it("pins the reader's pick on the create, not on the first message", async () => {
    const subject = build();
    await subject.source.createChat("count yesterday's orders", {
      model: { id: "model-alpha", displayName: "Model Alpha", wire: "anthropic", efforts: [], defaultEffort: null },
      effort: "high",
    });
    expect(subject.rest.createChat).toHaveBeenCalledWith("Count yesterday's orders", {
      model: "model-alpha",
      effort: "high",
      claimSpare: true,
    });
  });

  it("says plainly that a result is not in the cloud until it is saved", async () => {
    // Shown as the page's own copy: a sentence, capitalised, saying where the
    // result is and what opens it here.
    await expect(new CloudDataSource().fetchBlob()).rejects.toThrow(
      /^This result is on the machine that produced it\. Save it from the chat to open it here\.$/,
    );
  });
});


describe("CloudDataSource answers the asks a turn stops on", () => {
  // The verdict rides the chat row in the shared cache; each case starts with
  // no row read, as a fresh page does.
  beforeEach(() => queryClient.clear());

  // The ask is outstanding on the MACHINE, so answering it is a relay, not a
  // write: `POST /chats/{id}/answer` in the exact shape the mirror validates.
  it("relays a permission option under the id the ask was raised with", async () => {
    const subject = build();
    await subject.source.resolvePermission("c1", "perm-1", "allow_once");
    expect(subject.answerInterrupt).toHaveBeenCalledWith("c1", {
      interrupt_id: "perm-1",
      option_id: "allow_once",
      reject: false,
    });
  });

  it("relays a question's answers, one list per prompt", async () => {
    const subject = build();
    await subject.source.answerQuestion("c1", "q-1", [["Postgres"], ["Yesterday"]]);
    expect(subject.answerInterrupt).toHaveBeenCalledWith("c1", {
      interrupt_id: "q-1",
      answers: [["Postgres"], ["Yesterday"]],
      reject: false,
    });
  });

  it("relays a rejection with its reason", async () => {
    const subject = build();
    await subject.source.rejectQuestion("c1", "q-2", "not now");
    expect(subject.answerInterrupt).toHaveBeenCalledWith("c1", {
      interrupt_id: "q-2",
      reject: true,
      reason: "not now",
    });
  });

  // The machine discards an answer that would authorize a write on a session
  // that refuses writes, so the source has to read an ask the same way — a
  // surface offering an Allow the machine then dropped is a control that lies,
  // and one withholding an Allow the machine WOULD have honoured is a chat the
  // reader cannot unblock.
  const ask = (
    canonicalKind: string,
    subject?: { effect?: string },
  ): Parameters<CloudDataSource["mayAllow"]>[1] =>
    ({
      kind: "permission",
      requestId: "p",
      canonicalKind,
      permissionKind: canonicalKind,
      prompting: true,
      patterns: [],
      options: [],
      status: "pending",
      subject,
    }) as unknown as Parameters<CloudDataSource["mayAllow"]>[1];

  it.each([
    ["network", undefined, true],
    ["other", undefined, true],
    ["network", { effect: "read" }, true],
    ["edit", undefined, false],
    ["shell", undefined, false],
    ["task", undefined, false],
    ["external", undefined, false],
    ["network", { effect: "write" }, false],
    ["other", { effect: "destroy" }, false],
    ["other", { effect: "egress" }, false],
  ] as const)("in a read-only chat, mayAllow(%s, %o) is %s", async (kind, subject, allowed) => {
    const source = build().source;
    await source.getPermissionMode("c1");
    expect(source.mayAllow("c1", ask(kind, subject)).allowed).toBe(allowed);
  });

  // The server's sentence reaches the card as it said it.
  it("says the server's reason for a write a read-only chat discards", async () => {
    const source = build().source;
    await source.getPermissionMode("c1");
    expect(source.mayAllow("c1", ask("shell"))).toEqual({
      allowed: false,
      refusal: "This workspace is read-only, so it won't run this.",
    });
  });

  // Before the chat row is read there is no verdict to give. A card drawn from
  // the transcript in that moment offered no Allow AND said the workspace was
  // read-only, over a chat running in Default.
  it("offers no Allow, and names no stance, before the chat row is read", () => {
    const verdict = new CloudDataSource().mayAllow("c1", ask("shell"));
    expect(verdict).toEqual({ allowed: false, refusal: "Checking this chat's mode…" });
    expect(verdict).not.toEqual(expect.objectContaining({ refusal: expect.stringMatching(/read-only/) }));
  });

  // A reader in another tab moves the chat: the relay says so, the row the
  // verdict came from is for the old stance, and the card waits for the
  // server's word on the new one rather than keep the old.
  it("waits for the server's verdict on a stance the chat moved to", async () => {
    const subject = build();
    await subject.source.setPermissionMode("c1", "default");
    expect(subject.source.mayAllow("c1", ask("shell")).allowed).toBe(true);

    subject.stored.permission_mode = "plan";
    subject.source.subscribeChat("c1", () => {});
    subject.doc.op({ op_id: "srv-mode", intent: "user_message", events: [{ kind: "mode", mode: "plan" }] } as never);
    expect(subject.source.mayAllow("c1", ask("shell"))).toEqual(APPROVAL_PENDING);
    expect(queryClient.getQueryState(keys.chats.one("c1"))?.isInvalidated).toBe(true);

    // What the page's own read of the row does next.
    await queryClient.fetchQuery({ queryKey: keys.chats.one("c1"), queryFn: () => subject.getChat() });
    expect(subject.source.mayAllow("c1", ask("shell"))).toEqual({
      allowed: false,
      refusal: "Plan mode explores and proposes a plan, so it won't run this.",
    });
  });

  // The same asks, in the stance where the box DOES act on an approval. Without
  // this half, switching a chat to `default` would leave every write-class ask
  // showing "this workspace is read-only" over a session that is not.
  it.each([
    ["edit", undefined, true],
    ["shell", undefined, true],
    ["network", { effect: "write" }, true],
    ["other", { effect: "destroy" }, true],
  ] as const)("in a default chat, mayAllow(%s, %o) is %s", async (kind, subject, allowed) => {
    const source = build().source;
    await source.setPermissionMode("c1", "default");
    expect(source.mayAllow("c1", ask(kind, subject)).allowed).toBe(allowed);
  });

  // Plan explores without changing anything, so an approval there is discarded
  // by the box exactly as it is in read-only — the two must not be lumped in
  // with `default` just because both are "not read-only".
  it("refuses a write-class ask in a plan chat", async () => {
    const source = build().source;
    await source.setPermissionMode("c1", "plan");
    expect(source.mayAllow("c1", ask("shell")).allowed).toBe(false);
  });

  // Auto runs the recoverable middle on its own and PAUSES for the risky step —
  // the pause is the reader's to answer, and the box acts on what they say. A
  // card that refused it left the one stance built for an unattended run with no
  // way past its own floor.
  it.each([["edit"], ["shell"]] as const)(
    "in an auto chat an approval reaches the machine (%s)",
    async (kind) => {
      const source = build().source;
      await source.setPermissionMode("c1", "auto");
      expect(source.mayAllow("c1", ask(kind)).allowed).toBe(true);
    },
  );

  // An ask that still surfaces in `bypass` is one the box chose to raise, so
  // its Allow is live. Showing the read-only refusal over it would leave the
  // reader unable to release a turn on the very stance they picked to run the
  // work unattended.
  it.each([["edit"], ["shell"]] as const)(
    "in a bypass chat an approval reaches the machine (%s)",
    async (kind) => {
      const source = build().source;
      await source.setPermissionMode("c1", "bypass");
      expect(source.mayAllow("c1", ask(kind)).allowed).toBe(true);
    },
  );

  // The source follows every listed chat at once, so the stance is a fact about
  // a CHAT, never about the source. A chat left read-only must not inherit the
  // permission of one the reader switched.
  it("decides per chat, not per source", async () => {
    const source = build().source;
    await source.setPermissionMode("c1", "default");
    expect(source.mayAllow("c1", ask("shell")).allowed).toBe(true);
    expect(source.mayAllow("c2", ask("shell")).allowed).toBe(false);
  });
});

// A snapshot is how a chat OPENS, and it is also what every reconnect delivers
// — a pong timeout, the 50-minute session deadline, a backend restart. On a
// cold open nothing is outstanding, so settling the retained window is right:
// the machine that raised those asks is long gone. On a reconnect it is still
// there, holding the permission the reader was about to answer and streaming
// into the turn on screen. Settling THAT snapshot retires an ask nobody can
// raise again (the turn then runs to its wall clock) and flashes a live turn
// as cancelled.
describe("CloudDataSource on a reconnect", () => {
  const ASK = {
    event_type: "permission.request",
    request_id: "perm-1",
    permission_kind: "network",
    canonical_kind: "network",
    prompting: true,
    patterns: ["https://api.example.com/*"],
    subject: { capability: "http", effect: "read", targets: [] },
    options: [
      { option_id: "allow_once", name: "Allow once" },
      { option_id: "reject_once", name: "Reject" },
    ],
  };

  /** What the machine published during the turn — the same entries the server
   *  hands back in the retained window when the socket comes back. */
  const live = () => [
    docEntry("u1-c", { event_type: "message.created", message_id: "u1", role: "user" }),
    docEntry("u1-t", {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-p", message_id: "u1", type: "text", text: "read the sales API" },
    }),
    ...textEvents("a1", "on it").map((payload, index) => docEntry(`a1-${index}`, payload)),
    docEntry("perm-1", ASK),
  ];

  function midTurn() {
    const subject = build();
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([]);
    subject.doc.op({ op_id: "o1", intent: "append", events: live() });
    return subject;
  }

  it("keeps an ask the machine is still holding alive across a re-hello", async () => {
    const subject = midTurn();
    expect(pendingAskKind(await subject.source.getChatTurns("c1"))).toBe("permission");

    // The socket dropped and came back: the same document, re-delivered.
    subject.doc.snapshot(live());

    expect(pendingAskKind(await subject.source.getChatTurns("c1"))).toBe("permission");
  });

  it("does not flash the streaming turn as cancelled when the socket comes back", async () => {
    const subject = midTurn();

    subject.doc.snapshot(live());

    const turns = await subject.source.getChatTurns("c1");
    expect(turns[turns.length - 1]?.status).not.toBe("cancelled");
  });

  // A pending ask is state of the chat, not of the process that raised it: the
  // box that opens the chat next re-offers the same ask under the same id and
  // resolves the answer. So a cold open keeps the announced ask answerable
  // however old the machine's word on the turn is.
  it("keeps the announced ask alive when a chat is opened cold", async () => {
    const subject = build();
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot(live());

    expect(pendingAskKind(await subject.source.getChatTurns("c1"))).toBe("permission");
  });

  it("still retires an ask nobody announced when a chat is opened cold", async () => {
    // The harness's own entry, published before the box's policy ran: the
    // policy may already have answered it, so a card would be a lie.
    const unannounced = Object.fromEntries(
      Object.entries(ASK).filter(([key]) => key !== "prompting"),
    );
    const subject = build();
    subject.source.subscribeChat("c1", () => undefined);
    subject.doc.snapshot([...live().slice(0, -1), docEntry("perm-1", unannounced)]);

    expect(pendingAskKind(await subject.source.getChatTurns("c1"))).toBeNull();
  });
});
