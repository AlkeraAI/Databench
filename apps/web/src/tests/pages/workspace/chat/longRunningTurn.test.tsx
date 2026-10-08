// A turn that runs for hours, and the browser that must not call it dead.
//
// A chat turn may legitimately run for hours or days — a warehouse query, a
// compaction, a subagent exploring — and the browser has two clocks that used
// to end one on their own: the age of the machine's `working` stamp (a first
// snapshot past five minutes folded the running tool to failed and cancelled
// the turn under it), and a five-minute silence floor (which cancelled the turn
// locally and told the reader it had crashed). Neither is evidence of anything.
//
// What IS evidence is the machine's absence, so that is the only thing here
// that retires a turn: the same compute-plane status the unreachable banner is
// drawn from, handed down to the source by the page that reads it.
//
// This is the COLD OPEN half — a reader arriving on a turn already in flight.
// The silence floor under a live turn is pinned in ChatSurface.watchdog.

import { describe, expect, it, vi } from "vitest";

import type { DocHandle, DocMessage } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

/** When the box stamped `working` — and when the reader opens the chat. Six
 *  hours apart: a research turn, not a dead box. */
const STAMPED_AT = "2026-09-06T06:00:00Z";
const SIX_HOURS_LATER = "2026-09-06T12:00:00Z";

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

/** A chat document that replays only what a test pushes into it. */
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
    dispose: () => listeners.clear(),
  };
  return {
    handle,
    /** The publisher's word on the meta lane, as the document delivers it. */
    setMeta(meta: Record<string, unknown>) {
      listeners.forEach((listener) =>
        listener({
          kind: "op",
          ephemeral: false,
          peerId: "srv:1",
          epoch: 1,
          seq: 0,
          payload: { op_id: "op-meta", intent: "set_meta", meta },
        } as unknown as DocMessage<unknown>),
      );
    },
  };
}

/** One entry as `GET /chats/{id}/messages` serves it: the published event, one
 *  level in. */
function machineRow(seq: number, payload: Record<string, unknown>) {
  return {
    id: `row-${seq}`,
    seq,
    event_id: String(payload.event_id),
    role: "assistant",
    kind: String(payload.event_type),
    payload: {
      event_id: String(payload.event_id),
      role: "assistant",
      kind: String(payload.event_type),
      payload,
    },
    created_at: STAMPED_AT,
  };
}

/** The transcript of a turn the box is in the middle of: an assistant message
 *  and a tool call it has not come back from. */
const MID_TURN = [
  machineRow(1, {
    event_type: "turn.started",
    event_id: "ev-turn",
    turn_id: "t1",
    time: STAMPED_AT,
  }),
  machineRow(2, {
    event_type: "message.created",
    event_id: "ev-a1",
    message_id: "a1",
    role: "assistant",
    time: STAMPED_AT,
  }),
  machineRow(3, {
    event_type: "tool.call",
    event_id: "ev-tool",
    message_id: "a1",
    tool_call_id: "tool-1",
    tool_name: "warehouse_query",
    status: "running",
    input: { sql: "select * from orders" },
    time: STAMPED_AT,
  }),
];

/** The real source over a chat opened COLD, six hours after the box last said
 *  it was working, on a machine that is still there. */
function chatReopenedOn() {
  const doc = fakeDoc();
  const source = new CloudDataSource({
    rest: {
      listChats: async () => ({
        items: [
          {
            id: "c1",
            title: "Warehouse scan",
            machine_id: "m1",
            machine_status: "ready",
            created_at: STAMPED_AT,
            updated_at: SIX_HOURS_LATER,
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
  doc.setMeta({
    permission_mode: "default",
    turn_state: { state: "working", at: STAMPED_AT },
  });
  return source;
}

// No clock is pinned: the wall clock is already far past the stamp, which is
// the whole point — the age of the machine's last word decides nothing.

describe("a chat reopened six hours into a turn", () => {
  it("keeps the turn working while the machine is there", async () => {
    const source = chatReopenedOn();

    const turns = await source.getChatTurns("c1");

    const tool = turns.flatMap((turn) => turn.parts).find((part) => part.kind === "tool");
    expect(tool, "the tool the box is still inside").toBeDefined();
    expect(tool?.kind === "tool" ? tool.state : null).toBe("running");
    expect(turns.map((turn) => turn.status)).not.toContain("cancelled");
  });
});
