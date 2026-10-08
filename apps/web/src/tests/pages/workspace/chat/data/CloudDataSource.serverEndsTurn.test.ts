// A turn nobody is left to finish ends where the reader is looking, on the
// server's word.
//
// When the box is reaped mid-answer, the browser does not decide the turn is
// over from the compute plane (only the web reads it, so the other shells would
// keep "Working…"). The server ends the turn (`turn_state: idle` on the
// document, with a line saying why), and the source closes whatever the box
// left open the moment that word arrives, with no reload.

import { describe, expect, it, vi } from "vitest";

import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";

/** The chat's document, with the publisher's meta lane. */
function doc() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  const open = (): DocHandle<unknown> => ({
    onMessage: (listener) => {
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
  });
  return {
    open,
    /** A box mid-answer: one message started, one chunk that is not the last,
     *  and the publisher's own word that the turn is running. */
    answering(): void {
      const events = [
        { event_type: "message.created", message_id: "a1", role: "assistant" },
        {
          event_type: "agent.message_chunk",
          message_id: "a1",
          part_id: "a1-t",
          text: "Looking at the poem",
          is_final: false,
        },
      ];
      listeners.forEach((l) =>
        l({
          kind: "snapshot",
          state: { events, meta: { turn_state: { state: "working" } } },
          epoch: 1,
          seq: 0,
        }),
      );
    },
    /** A turn-state write on the meta lane, as the box or the server makes it. */
    turn(state: "working" | "idle"): void {
      listeners.forEach((l) =>
        l({
          kind: "op",
          payload: { op_id: `m-${state}`, intent: "set_meta", meta: { turn_state: { state } } },
          peerId: "server",
          epoch: 1,
          seq: 1,
          ephemeral: false,
        }),
      );
    },
  };
}

function scene() {
  const document = doc();
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

const streamingParts = (turns: { parts: { streaming?: boolean }[] }[]): number =>
  turns.reduce((n, turn) => n + turn.parts.filter((p) => p.streaming === true).length, 0);

describe("a chat whose turn the server ends while it is answering", () => {
  it("closes what the box was streaming and stops reading as working, with no reload", async () => {
    const { source, document } = scene();
    const events: unknown[] = [];
    source.subscribeChat("c1", (event) => events.push(event));
    document.answering();

    expect(source.turnState("c1")).toBe("working");
    expect(streamingParts(await source.getChatTurns("c1"))).toBe(1);
    const seen = events.length;

    document.turn("idle");

    expect(streamingParts(await source.getChatTurns("c1"))).toBe(0);
    expect(source.turnState("c1")).toBe("idle");
    // The reader is watching: the transcript is told to re-read.
    expect(events.length).toBeGreaterThan(seen);
  });

  it("leaves a running turn alone until the document says it is over", async () => {
    // Nothing but the document's word ends a turn: a turn may run for hours,
    // and a box that missed its heartbeats comes back holding it.
    const { source, document } = scene();
    source.subscribeChat("c1", () => undefined);
    document.answering();

    document.turn("working");

    expect(source.turnState("c1")).toBe("working");
    expect(streamingParts(await source.getChatTurns("c1"))).toBe(1);
  });

  it("closes nothing for a chat it is not following", async () => {
    const { source } = scene();
    const listener = vi.fn();
    source.subscribeChat("c2", listener);

    expect(source.turnState("c1")).toBeNull();
    expect(listener).not.toHaveBeenCalled();
  });
});
