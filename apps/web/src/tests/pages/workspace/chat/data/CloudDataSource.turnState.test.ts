// What the browser knows about a turn it cannot see, and how it says a turn was
// lost.
//
// The portal's agent runs on a machine in another datacentre. Between one model
// step and the next it emits nothing for a minute or more, and the only thing
// that distinguishes that from a crash is the publisher's own word — which rides
// the chat document's META lane (`{"turn_state": {"state": "working"}}`), not
// the transcript. The source hears it here so the stall watchdog can ask about
// liveness instead of counting silence.
//
// The same distance decides the WORDS for a real crash: a reader whose agent
// died on a box they do not own is told the workspace stopped answering, not to
// go and check a backend and a model gateway.

import { describe, expect, it } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { WORKSPACE_STOPPED_ANSWERING } from "@/pages/workspace/chat/data/harnessEventFold";
import type { DocHandle, DocMessage, OpPayload } from "@/api/realtime/docSync";

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
    snapshot(state: Record<string, unknown>): void {
      listeners.forEach((l) => l({ kind: "snapshot", state, epoch: 1, seq: 0 }));
    },
    op(payload: OpPayload): void {
      listeners.forEach((l) =>
        l({ kind: "op", payload, peerId: "pub:1", epoch: 1, seq: 1, ephemeral: false }),
      );
    },
  };
}

function build() {
  const doc = fakeDoc();
  const rest = {
    listMessages: async () => ({ items: [], next_after_seq: 0, resync_from: null }),
  };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => doc.handle as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  return { source, doc };
}

/** The publisher's meta op, exactly as `_queue_meta` builds it. */
function turnMeta(state: "working" | "idle"): OpPayload {
  return {
    op_id: "op-1",
    intent: "set_meta",
    meta: { turn_state: { state, at: "2026-09-07T03:00:00Z" }, turn_state_at: "2026-09-07T03:00:00Z" },
  };
}

describe("the machine's word on the turn", () => {
  it("is nothing until the document says something", () => {
    const { source } = build();
    source.subscribeChat("c1", () => undefined);
    expect(source.turnState("c1")).toBeNull();
  });

  it("reads `working` off the publisher's meta op", () => {
    const { source, doc } = build();
    source.subscribeChat("c1", () => undefined);
    doc.snapshot({ events: [] });

    doc.op(turnMeta("working"));

    expect(source.turnState("c1")).toBe("working");
  });

  it("reads it off the hello for a reader who joined mid-turn", () => {
    const { source, doc } = build();
    source.subscribeChat("c1", () => undefined);
    doc.snapshot({ events: [], meta: { turn_state: { state: "working" } } });

    expect(source.turnState("c1")).toBe("working");
  });

  it("goes idle when the turn ends", () => {
    const { source, doc } = build();
    source.subscribeChat("c1", () => undefined);
    doc.op(turnMeta("working"));
    doc.op(turnMeta("idle"));

    expect(source.turnState("c1")).toBe("idle");
  });

  it("is not retired by an unrelated meta write", () => {
    // The server MERGES meta, so a frame that carries some other key says
    // nothing about the turn — and must not read as "the turn ended", which
    // would hand the watchdog back its silence timer mid-answer.
    const { source, doc } = build();
    source.subscribeChat("c1", () => undefined);
    doc.op(turnMeta("working"));
    doc.op({ op_id: "op-2", intent: "set_meta", meta: { title: "Weekly order volume" } });

    expect(source.turnState("c1")).toBe("working");
  });

  it("outlives the socket that heard it", () => {
    // The word is about the MACHINE, not about this connection. A box mid-turn
    // keeps writing while the tab is shut, so letting go of the chat may not
    // read as "the turn ended" — the reconnect that follows folds its snapshot
    // as a replay, and a replay with no standing `working` to stop it settles
    // the running turn as cancelled.
    const { source, doc } = build();
    const off = source.subscribeChat("c1", () => undefined);
    doc.op(turnMeta("working"));
    off();

    expect(source.turnState("c1")).toBe("working");
  });

  it("is retired by the box's own word after the reader comes back", () => {
    // The other side of it: nothing is pinned open. The turn ends when the
    // machine says it ended.
    const { source, doc } = build();
    const off = source.subscribeChat("c1", () => undefined);
    doc.op(turnMeta("working"));
    off();
    source.subscribeChat("c1", () => undefined);
    doc.op(turnMeta("idle"));

    expect(source.turnState("c1")).toBe("idle");
  });
});

describe("an agent that died under the turn", () => {
  it("is reported as the WORKSPACE going quiet, not as a backend to go and check", async () => {
    const { source, doc } = build();
    source.subscribeChat("c1", () => undefined);
    // The daemon's synthetic crash detail, as the machine publishes it when the
    // agent process is killed out from under a turn.
    doc.snapshot({
      events: [
        {
          event_id: "e1",
          role: "assistant",
          kind: "session.status_changed",
          payload: {
            event_type: "session.status_changed",
            status: "error",
            detail: "agent exited unexpectedly (rc=-15)",
          },
        },
      ],
    });

    const turns = await source.getChatTurns("c1");
    const said = turns.flatMap((turn) =>
      turn.parts.flatMap((part) => (part.kind === "system" ? [part.text] : [])),
    );
    expect(said).toContain(WORKSPACE_STOPPED_ANSWERING);
    expect(said.join(" ")).not.toContain("model gateway");
  });
});
