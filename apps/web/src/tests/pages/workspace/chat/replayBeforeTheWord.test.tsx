// A durable read that lands before the socket's first word about the turn.
//
// A page reload mid-turn opens the chat twice at once: the REST page (the
// durable record) and the document socket (whose snapshot carries the box's
// `turn_state`). Whichever lands first, the browser must render the same chat
// the box is running — the source of truth is the server's snapshot plus its
// ordered events, and the client derives everything from them. It never
// invents a terminal state because a read arrived in a particular order.
//
// This pins the race: with NO word yet, the cold open must not retire the
// running tool as failed and stamp the turn cancelled, or the tool's real
// completion lands on a turn the browser already closed. The idle half is
// pinned too: the
// same read, after the box has said `idle`, does settle, because that is the
// box's word and not the browser's guess.

import { describe, expect, it, vi } from "vitest";

import type { DocHandle, DocMessage } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

const STAMPED_AT = "2026-09-21T06:00:00Z";

const { installed } = vi.hoisted(() => ({ installed: { source: null as unknown } }));

vi.mock("@/pages/workspace/chat/data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
  chatHost: () => ({
    kind: "browser" as const,
    engine: {
      request: () => Promise.reject(new Error("the browser portal has no engine channel")),
    },
    runCommand: async () => {},
    openFile: async () => {},
    subscribe: () => () => {},
    workspacePath: () => null,
    openPlanDocument: async () => {},
    account: () => ({ email: "analyst@tideline.example", webAppUrl: null }),
    onAccountChange: () => () => {},
    saveFile: async () => {},
    auth: { openBrowser: () => {} },
  }),
  chatData: () => installed.source,
  chatCaps: () => ({ opencodeActive: false }),
  refetchWhileErrored: () => false as const,
  refetchWhileNoChatDefault: () => false as const,
  refetchWhileErroredOrEmpty: () => false as const,
}));

/** A chat document that delivers only what a test pushes into it. */
function fakeDoc() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  let seq = 0;
  const deliver = (payload: Record<string, unknown>, ephemeral = false) => {
    seq += 1;
    listeners.forEach((listener) =>
      listener({
        kind: "op",
        ephemeral,
        peerId: "srv:1",
        epoch: 1,
        seq,
        payload: { op_id: `op-${seq}`, ...payload },
      } as unknown as DocMessage<unknown>),
    );
  };
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
    dispose: () => listeners.clear(),
  };
  return {
    handle,
    /** The publisher's word on the meta lane. */
    setMeta: (meta: Record<string, unknown>) => deliver({ intent: "set_meta", meta }),
    /** A transcript entry the publisher appended. */
    append: (entry: Record<string, unknown>) => deliver({ intent: "append", events: [entry] }),
  };
}

/** One transcript entry as the publisher writes it: the harness event one level in. */
function entry(seq: number, payload: Record<string, unknown>) {
  return {
    seq,
    event_id: String(payload.event_id),
    role: "assistant",
    kind: String(payload.event_type),
    payload,
  };
}

/** The same entry as `GET /chats/{id}/messages` serves it. */
function row(seq: number, payload: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    seq,
    event_id: String(payload.event_id),
    role: "assistant",
    kind: String(payload.event_type),
    payload: entry(seq, payload),
    created_at: STAMPED_AT,
  };
}

const TOOL_CALL = {
  event_type: "tool.call",
  event_id: "ev-tool",
  message_id: "a1",
  tool_call_id: "tool-1",
  tool_name: "write",
  status: "running",
  input: { path: "report.md" },
  time: STAMPED_AT,
};

/** A turn the box is inside: an assistant message and a write it has not come
 *  back from. */
const MID_TURN = [
  row(1, { event_type: "turn.started", event_id: "ev-turn", turn_id: "t1", time: STAMPED_AT }),
  row(2, {
    event_type: "message.created",
    event_id: "ev-a1",
    message_id: "a1",
    role: "assistant",
    time: STAMPED_AT,
  }),
  row(3, TOOL_CALL),
];

function chatOpened() {
  const doc = fakeDoc();
  const source = new CloudDataSource({
    rest: {
      listChats: async () => ({
        items: [
          {
            id: "c1",
            title: "Report",
            machine_id: "m1",
            machine_status: "ready",
            created_at: STAMPED_AT,
            updated_at: STAMPED_AT,
            last_seq: MID_TURN.length,
          },
        ],
        next_cursor: null,
      }),
      createChat: vi.fn(),
      listMessages: async () => ({ items: MID_TURN, next_after_seq: null, resync_from: null }),
      postMessage: vi.fn(),
      answerInterrupt: vi.fn(),
    } as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  installed.source = source;
  source.subscribeChat("c1", () => undefined);
  return { source, doc };
}

function toolOf(turns: Awaited<ReturnType<CloudDataSource["getChatTurns"]>>) {
  return turns.flatMap((turn) => turn.parts).find((part) => part.kind === "tool");
}

describe("a durable read that lands before the box's word about the turn", () => {
  it("retires nothing: the write stays running and the turn stays open", async () => {
    const { source } = chatOpened();

    const turns = await source.getChatTurns("c1");

    const tool = toolOf(turns);
    expect(tool?.kind === "tool" ? tool.state : null).toBe("running");
    expect(turns.map((turn) => turn.status)).not.toContain("cancelled");
  });

  it("lets the write's real completion land once the word and the result arrive", async () => {
    const { source, doc } = chatOpened();
    await source.getChatTurns("c1");

    doc.setMeta({ turn_state: { state: "working", at: STAMPED_AT } });
    doc.append(
      entry(4, {
        ...TOOL_CALL,
        event_type: "tool.call_update",
        event_id: "ev-tool-done",
        status: "completed",
        output: "wrote report.md",
      }),
    );

    const turns = await source.getChatTurns("c1");
    const tool = toolOf(turns);
    expect(tool?.kind === "tool" ? tool.state : null).toBe("completed");
    // Had the cold open stamped the turn cancelled, the write's completion
    // would land on a closed turn: the card reads written, the turn under it
    // still reads cancelled, and the composer is free while the box works on.
    expect(turns.map((turn) => turn.status)).not.toContain("cancelled");
  });

  it("settles the same read once the box has said the turn is over", async () => {
    const { source, doc } = chatOpened();
    doc.setMeta({ turn_state: { state: "idle", at: STAMPED_AT } });

    const turns = await source.getChatTurns("c1");

    const tool = toolOf(turns);
    // This is the box's word, not the browser's: the box closed the turn
    // without closing the write, so the write is over.
    expect(tool?.kind === "tool" ? tool.state : null).toBe("error");
    expect(turns.map((turn) => turn.status)).toContain("cancelled");
  });
});
