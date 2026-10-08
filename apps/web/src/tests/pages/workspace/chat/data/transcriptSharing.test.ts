// What a streamed token costs the reader's tab.
//
// A long chat's loaded window holds thousands of turns, and the publisher
// flushes a frame of tokens up to twenty times a second. Handing the store a
// fresh deep copy of the whole window on each of those frames put the cost of
// the WHOLE transcript behind every token — measured here at the window's own
// cap, where it dominated the frame. The snapshot a source hands out carries
// every turn the fold did not change over by identity, so a token costs one
// turn's copy and the blocks above it are not re-rendered.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import {
  TranscriptWindow,
  type TranscriptWindowAdapter,
  type WindowRow,
} from "@/pages/workspace/chat/data/transcriptWindow";

interface Row extends WindowRow {
  event: HarnessEvent;
}

const adapter: TranscriptWindowAdapter<Row> = {
  createState: () => createConversationFoldState(),
  foldRows: (state, rows) => {
    for (const row of rows) foldHarnessEvent(state, row.event);
  },
  startsTurn: (row) => row.event.event_type === "message.created" && row.event.role === "user",
  elidedTurnIds: (row) =>
    row.event.event_type === "compaction.applied"
      ? ((row.event.summarised_message_ids as string[] | undefined) ?? [])
      : [],
};

let seq = 0;

function row(eventId: string, event: HarnessEvent, at?: number): Row {
  return { seq: at ?? ++seq, eventId, event };
}

/** One finished exchange, as the rows a window folds. */
function exchange(n: number): Row[] {
  return [
    row(`u${n}`, { event_type: "message.created", event_id: `u${n}`, message_id: `u${n}`, role: "user" }),
    row(`up${n}`, {
      event_type: "part.created",
      event_id: `up${n}`,
      message_id: `u${n}`,
      part: { part_id: `u${n}-t`, message_id: `u${n}`, type: "text", text: `ask ${n}` },
    }),
    row(`a${n}`, { event_type: "message.created", event_id: `a${n}`, message_id: `a${n}`, role: "assistant" }),
    row(`ap${n}`, {
      event_type: "part.created",
      event_id: `ap${n}`,
      message_id: `a${n}`,
      part: { part_id: `a${n}-t`, message_id: `a${n}`, type: "text", text: `reply ${n}` },
    }),
  ];
}

/** A window holding `exchanges` finished exchanges, ready to stream into. */
function loaded(exchanges: number): TranscriptWindow<Row> {
  seq = 0;
  const window = new TranscriptWindow<Row>(adapter);
  const rows: Row[] = [];
  for (let n = 0; n < exchanges; n++) rows.push(...exchange(n));
  window.appendLive(rows);
  return window;
}

/** How many turns changed object identity between two snapshots. */
function moved(before: readonly unknown[], after: readonly unknown[]): number {
  let count = 0;
  for (let i = 0; i < after.length; i++) if (before[i] !== after[i]) count += 1;
  return count;
}

describe("a snapshot of the loaded window", () => {
  it("carries every turn a streamed token did not change over by identity", () => {
    const window = loaded(2500);
    // The chat is streaming a reply into the newest exchange.
    window.appendLive([
      row("live", { event_type: "message.created", event_id: "live", message_id: "live", role: "assistant" }),
    ]);
    let previous = window.snapshot();
    expect(previous).toHaveLength(5001);

    for (let i = 0; i < 1000; i++) {
      window.appendLive([
        row(`c${i}`, {
          event_type: "agent.message_chunk",
          event_id: `c${i}`,
          message_id: "live",
          part_id: "live-p",
          text: "tok ",
        }),
      ]);
      const next = window.snapshot();
      // One token, one turn: the 5000 turns above the live one are the very
      // objects the previous frame rendered, so React re-renders one block.
      expect(moved(previous, next)).toBe(1);
      expect(next[next.length - 1]).not.toBe(previous[previous.length - 1]);
      previous = next;
    }

    const streamed = previous[previous.length - 1] as { parts: { text: string }[] };
    expect(streamed.parts[0].text).toBe("tok ".repeat(1000));
  });

  it("hands out copies the caller cannot write back into the fold", () => {
    const window = loaded(2);
    const snapshot = window.snapshot();
    expect(snapshot[0]).not.toBe(window.turns()[0]);
    (snapshot[0] as { status: string }).status = "error";
    expect(window.turns()[0].status).not.toBe("error");
    // …and once the fold does move that turn, what comes back is the fold's
    // word, not what the caller wrote over it.
    window.appendLive([
      row("up0b", {
        event_type: "part.created",
        event_id: "up0b",
        message_id: "u0",
        part: { part_id: "u0-t2", message_id: "u0", type: "text", text: "more" },
      }),
    ]);
    expect((window.snapshot()[0] as { status: string }).status).not.toBe("error");
  });

  it("re-copies a turn whose fold changed without a token of its own", () => {
    const window = loaded(3);
    const before = window.snapshot();
    // A tool result lands on the newest assistant turn through a path that
    // names no turn — the conservative default has to re-copy it.
    window.appendLive([
      row("tc", {
        event_type: "tool.call",
        event_id: "tc",
        message_id: "a2",
        tool_call_id: "call1",
        tool_name: "bash",
        tool_kind: "terminal",
        status: "running",
        input: { command: "echo hi" },
      }),
    ]);
    const after = window.snapshot();
    expect(after[after.length - 1]).not.toBe(before[before.length - 1]);
    const parts = (after[after.length - 1] as { parts: unknown[] }).parts;
    expect(parts.length).toBeGreaterThan(1);
  });

  it("re-copies the turns a compaction below the window dimmed", () => {
    const window = loaded(4);
    const before = window.snapshot();
    expect(before.every((turn) => (turn as { cleared?: boolean }).cleared !== true)).toBe(true);
    window.appendLive([
      row("comp", {
        event_type: "compaction.applied",
        event_id: "comp",
        summarised_message_ids: ["u0", "a0"],
      }),
    ]);
    const after = window.snapshot();
    const dimmed = after.filter((turn) => (turn as { cleared?: boolean }).cleared === true);
    expect(dimmed.map((turn) => (turn as { id: string }).id).sort()).toEqual(["a0", "u0"]);
    expect(after[0]).not.toBe(before[0]);
  });

  it("carries a page scrolled into above the window across the next token", () => {
    const window = loaded(3);
    // An older page reads below everything loaded.
    const older = [
      row("uo", { event_type: "message.created", event_id: "uo", message_id: "uo", role: "user" }, -10),
      row(
        "uop",
        {
          event_type: "part.created",
          event_id: "uop",
          message_id: "uo",
          part: { part_id: "uo-t", message_id: "uo", type: "text", text: "older" },
        },
        -9,
      ),
    ];
    window.prependOlder(older);
    window.appendLive([
      row("live2", { event_type: "message.created", event_id: "live2", message_id: "live2", role: "assistant" }),
    ]);
    const before = window.snapshot();
    window.appendLive([
      row("c", {
        event_type: "agent.message_chunk",
        event_id: "c",
        message_id: "live2",
        part_id: "live2-p",
        text: "x",
      }),
    ]);
    const after = window.snapshot();
    expect(after[0]).toBe(before[0]);
    expect(moved(before, after)).toBe(1);
  });

  it("forgets the turns a released page took with it", () => {
    const window = loaded(3);
    const older = [
      row("uo", { event_type: "message.created", event_id: "uo", message_id: "uo", role: "user" }, -10),
      row(
        "uop",
        {
          event_type: "part.created",
          event_id: "uop",
          message_id: "uo",
          part: { part_id: "uo-t", message_id: "uo", type: "text", text: "older" },
        },
        -9,
      ),
    ];
    window.prependOlder(older);
    expect(window.snapshot()).toHaveLength(7);
    expect(window.releaseHead(0).released).toBe(older.length);
    const after = window.snapshot();
    expect(after).toHaveLength(6);
    expect(after.map((turn) => (turn as { id: string }).id)).not.toContain("uo");
  });
});
