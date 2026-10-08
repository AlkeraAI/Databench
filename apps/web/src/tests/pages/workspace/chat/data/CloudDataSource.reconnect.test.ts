// A turn survives the socket that was watching it.
//
// The owner left the tab, the socket closed, and the owner came back: the
// browser minted a new ticket and re-opened the chat. That re-open is a COLD
// open by shape — the first snapshot on a fresh handle — but the machine on the
// other side never stopped: it was three seconds into a `write` that finished
// 58 seconds later.
//
// Settling that snapshot marks the still-pending write as FAILED and stamps the
// turn `cancelled`, and the events the box goes on publishing then land on a
// turn the browser has already closed. Nothing was cancelled anywhere but here.
//
// What must hold: the publisher's last word about the turn is re-established
// from the snapshot BEFORE the snapshot is judged, so a reconnect over a
// working machine retires nothing — while a machine that is GONE still settles.

import { describe, expect, it } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle, DocMessage, OpPayload } from "@/api/realtime/docSync";

/** The chat's document. Every `openDoc` hands out a FRESH handle over the same
 *  wire, which is what a reconnect is: the source's per-socket state starts
 *  again, the server's retained document does not. */
function wire() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  const open = (): DocHandle<unknown> => {
    const mine = new Set<(m: DocMessage<unknown>) => void>();
    return {
      onMessage: (listener) => {
        mine.add(listener);
        listeners.add(listener);
        return () => {
          mine.delete(listener);
          listeners.delete(listener);
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
    snapshot(state: Record<string, unknown>): void {
      listeners.forEach((l) => l({ kind: "snapshot", state, epoch: 1, seq: 0 }));
    },
    op(payload: OpPayload): void {
      listeners.forEach((l) =>
        l({ kind: "op", payload, peerId: "pub:1", epoch: 1, seq: 9, ephemeral: false }),
      );
    },
  };
}

/** The retained window the server replays on every hello: an assistant turn
 *  that has opened a `write` and not answered it. The call carries NO input yet
 *  — the box streams the arguments after the call id — which is exactly the
 *  state the reader saw as "✕ 1 failed — Wrote", with no path on the card. */
const WINDOW = [
  { event_id: "e1", seq: 1, role: "user", event_type: "message.created", message_id: "u1" },
  {
    event_id: "e2",
    seq: 2,
    role: "user",
    event_type: "part.created",
    message_id: "u1",
    part: { part_id: "u1-t", message_id: "u1", type: "text", text: "write the file" },
  },
  {
    event_id: "e3",
    seq: 3,
    role: "assistant",
    event_type: "message.created",
    message_id: "a1",
    role_name: "assistant",
  },
  {
    event_id: "e4",
    seq: 4,
    role: "assistant",
    event_type: "tool.call",
    message_id: "a1",
    tool_call_id: "call-w",
    name: "write",
    input: {},
  },
];

/** The publisher's own word, on the meta lane the snapshot carries. */
const WORKING = { turn_state: { state: "working", at: "2026-09-21T04:25:00Z" } };

function scene() {
  const document = wire();
  const source = new CloudDataSource({
    rest: {
      listMessages: async () => ({ items: [], next_after_seq: 0, resync_from: null }),
    } as never,
    openDoc: () => document.open() as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client",
  });
  return { source, document };
}

type Part = { kind: string; state?: string; streaming?: boolean };
type Turn = { author: string; status?: string; parts: Part[] };

const writeCall = (turns: Turn[]): Part | undefined =>
  turns.flatMap((t) => t.parts).find((p) => p.kind === "tool");

const assistant = (turns: Turn[]): Turn | undefined =>
  [...turns].reverse().find((t) => t.author === "assistant");

describe("a chat re-opened while its machine is still working", () => {
  it("keeps the pending tool call pending and the turn open, and the late result lands on it", async () => {
    const { source, document } = scene();
    const first = source.subscribeChat("c1", () => undefined);
    document.snapshot({ events: WINDOW, meta: WORKING });

    expect(writeCall((await source.getChatTurns("c1")) as Turn[])?.state).toBe("pending");
    expect(source.turnState("c1")).toBe("working");

    // The tab went away: the socket closed and every per-socket fact the source
    // kept about this chat went with it.
    first();

    // The owner came back. A new ticket, a new socket, and the server replays
    // the SAME retained window. The meta is MERGED server-side and the
    // publisher re-stamps the turn on its own heartbeat, so the frame that
    // opens the new socket need not re-state the turn at all — this one
    // carries the mode and nothing else. The word the source already had is
    // the only word there is, and closing the socket must not have erased it.
    source.subscribeChat("c1", () => undefined);
    document.snapshot({ events: WINDOW, meta: { permission_mode: "default" } });

    const after = (await source.getChatTurns("c1")) as Turn[];
    // Nothing was cancelled on the box, so nothing may be cancelled here: the
    // write is still out, and the turn is still the one the box is writing to.
    expect(writeCall(after)?.state).toBe("pending");
    expect(assistant(after)?.status).not.toBe("cancelled");
    expect(source.turnState("c1")).toBe("working");

    // 58 seconds later the write comes back. It must land on the turn that is
    // still open — not on a turn the browser closed while the box was working.
    document.op({
      op_id: "op-2",
      intent: "append",
      events: [
        {
          event_id: "e5",
          seq: 5,
          role: "assistant",
          payload: {
            event_type: "tool.call_update",
            message_id: "a1",
            tool_call_id: "call-w",
            status: "completed",
            output: { path: "notes.md" },
          },
        },
      ],
    } as OpPayload);

    const done = (await source.getChatTurns("c1")) as Turn[];
    expect(writeCall(done)?.state).toBe("completed");
    expect(assistant(done)?.status).not.toBe("cancelled");
  });

  it("settles a re-opened chat whose machine said the turn had ended", async () => {
    // The negative that keeps the fix honest: `idle` is the box's own word that
    // the turn is over, and a reconnect that reads it settles exactly as a cold
    // open does. Only `working` is protected.
    const { source, document } = scene();
    const first = source.subscribeChat("c1", () => undefined);
    document.snapshot({ events: WINDOW, meta: WORKING });
    first();

    source.subscribeChat("c1", () => undefined);
    document.snapshot({
      events: WINDOW,
      meta: { turn_state: { state: "idle", at: "2026-09-21T04:26:00Z" } },
    });

    const after = (await source.getChatTurns("c1")) as Turn[];
    expect(writeCall(after)?.state).toBe("error");
    expect(assistant(after)?.status).toBe("cancelled");
  });
});
