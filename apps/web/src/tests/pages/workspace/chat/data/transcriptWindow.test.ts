// The loaded window joins its pages into the transcript a single fold would
// have produced — whichever way the pages were cut.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type ConversationFoldState,
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

/** Every event carries its own ids so a fold is deterministic. */
function turnEvents(n: number, opts: { pendingAsk?: boolean; running?: boolean } = {}): HarnessEvent[] {
  const events: HarnessEvent[] = [
    { event_type: "message.created", event_id: `u${n}`, message_id: `u${n}`, role: "user" },
    {
      event_type: "part.created",
      event_id: `u${n}-p`,
      message_id: `u${n}`,
      part: { part_id: `u${n}-text`, message_id: `u${n}`, type: "text", text: `ask ${n}` },
    },
    { event_type: "turn.started", event_id: `ts${n}`, turn_id: `turn${n}` },
    { event_type: "message.created", event_id: `a${n}`, message_id: `a${n}`, role: "assistant" },
    {
      event_type: "tool.call",
      event_id: `tc${n}`,
      message_id: `a${n}`,
      tool_call_id: `call${n}`,
      tool_name: "bash",
      tool_kind: "terminal",
      status: "running",
      input: { command: `echo ${n}` },
    },
  ];
  if (opts.pendingAsk) {
    events.push({
      event_type: "permission.request",
      event_id: `pr${n}`,
      request_id: `req${n}`,
      permission_kind: "bash",
      prompting: true,
      options: [{ option_id: "allow_once", name: "Allow" }],
    });
    return events;
  }
  if (!opts.running) {
    events.push(
      { event_type: "tool.call_update", event_id: `tu${n}`, tool_call_id: `call${n}`, status: "completed", output: `${n}` },
      {
        event_type: "part.created",
        event_id: `a${n}-p`,
        message_id: `a${n}`,
        part: { part_id: `a${n}-text`, message_id: `a${n}`, type: "text", text: `answer ${n}` },
      },
      // Stamped like the harness stamps it: a finished turn carries the time it
      // ended, which is what tells a settle it was not abandoned mid-stream.
      {
        event_type: "turn.finished",
        event_id: `tf${n}`,
        stop_reason: "complete",
        summary: `done ${n}`,
        time: `2026-01-01T00:00:0${n}.000Z`,
      },
    );
  }
  return events;
}

function rows(events: HarnessEvent[], startSeq = 1): Row[] {
  return events.map((event, index) => ({ seq: startSeq + index, eventId: event.event_id as string, event }));
}

/** A live row nothing has stamped: an event id, and no sequence. That is the
 *  ephemeral token-chunk lane, and any frame from a source that does not name
 *  its ordinal — the cloud's server stamps an accepted append and the daemon
 *  stamps from its own log, so neither durable lane looks like this. */
function unstamped(events: HarnessEvent[]): Row[] {
  return events.map((event) => ({ seq: null, eventId: event.event_id as string, event }));
}

/** A streamed token as it arrives on the ephemeral lane: no sequence, no id to
 *  de-duplicate by, and superseded by the part the publisher writes when the
 *  text settles. */
function chunkEvent(n: number): HarnessEvent {
  return {
    event_type: "agent.message_chunk",
    message_id: `a${n}`,
    part_id: `a${n}-stream`,
    delta: "thinking",
  };
}

function wholeFold(all: Row[]): ConversationFoldState {
  const state = createConversationFoldState();
  for (const row of all) foldHarnessEvent(state, row.event);
  return state;
}

/** A settled page writes `streaming: false` where an unsettled fold leaves the
 *  key absent; the two render identically, so the comparison reads them as one. */
function settled(turns: unknown): unknown {
  return JSON.parse(
    JSON.stringify(turns, (key, value: unknown) => (key === "streaming" && value === false ? undefined : value)),
  );
}

const THREE_TURNS = rows([...turnEvents(1), ...turnEvents(2), ...turnEvents(3)]);

describe("a window over pages", () => {
  it("folds pages cut at turn starts into independent segments and reads them in order", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(2 * perTurn));
    window.prependOlder(THREE_TURNS.slice(perTurn, 2 * perTurn));
    window.prependOlder(THREE_TURNS.slice(0, perTurn));
    expect(window.segmentCount).toBe(3);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u1", "a1", "u2", "a2", "u3", "a3"]);
    expect(window.oldestSeq).toBe(1);
    expect(window.rowCount).toBe(THREE_TURNS.length);
  });

  it.each(Array.from({ length: THREE_TURNS.length - 1 }, (_, index) => index + 1))(
    "reads the same transcript as one fold when the page cut falls at row %i",
    (cut) => {
      const window = new TranscriptWindow(adapter);
      window.appendLive(THREE_TURNS.slice(cut));
      const { folded } = window.prependOlder(THREE_TURNS.slice(0, cut));
      expect(folded).toBe(cut);
      expect(settled(window.turns())).toEqual(settled(wholeFold(THREE_TURNS).turns));
      expect(window.oldestSeq).toBe(1);
    },
  );

  it("reads the same transcript as one fold across three pages cut inside turns", () => {
    const window = new TranscriptWindow(adapter);
    window.appendLive(THREE_TURNS.slice(20));
    window.prependOlder(THREE_TURNS.slice(11, 20));
    window.prependOlder(THREE_TURNS.slice(3, 11));
    window.prependOlder(THREE_TURNS.slice(0, 3));
    expect(settled(window.turns())).toEqual(settled(wholeFold(THREE_TURNS).turns));
  });

  it("completes a tool call whose result landed on the page below", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    // Cut turn 2 right after its tool.call: the call is on the older page, the
    // update on the newer one.
    const cut = perTurn + 5;
    window.appendLive(THREE_TURNS.slice(cut));
    window.prependOlder(THREE_TURNS.slice(0, cut));
    const a2 = window.turns().find((turn) => turn.id === "a2");
    const tool = a2?.parts.find((part) => part.kind === "tool");
    expect(tool && tool.kind === "tool" ? tool.state : null).toBe("completed");
    expect(window.turns().filter((turn) => turn.id === "a2")).toHaveLength(1);
  });

  it("reports whether the live segment itself was re-folded", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(2 * perTurn + 3));
    expect(window.prependOlder(THREE_TURNS.slice(perTurn, 2 * perTurn + 3)).liveRefolded).toBe(true);
    expect(window.prependOlder(THREE_TURNS.slice(0, perTurn)).liveRefolded).toBe(false);
  });
});

describe("what a page brings and what it must not", () => {
  it("never folds a row twice, from a page or from the live tail", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(2 * perTurn));
    expect(window.appendLive(THREE_TURNS.slice(2 * perTurn))).toBe(0);
    const page = THREE_TURNS.slice(perTurn, 2 * perTurn);
    expect(window.prependOlder(page).folded).toBe(page.length);
    expect(window.prependOlder(page).folded).toBe(0);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u2", "a2", "u3", "a3"]);
  });

  it("drops a live frame whose sequence sits below the window", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(2 * perTurn));
    // A snapshot that reaches further back than the tail page the reader opened
    // on: its older entries belong to a page not yet asked for.
    expect(window.appendLive(THREE_TURNS.slice(0, perTurn))).toBe(0);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u3", "a3"]);
    // …and that page still lands whole when it is asked for.
    expect(window.prependOlder(THREE_TURNS.slice(perTurn, 2 * perTurn)).folded).toBe(perTurn);
  });

  it("folds a live frame that carries no sequence as the newest thing", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(0, 3 * perTurn - 3));
    const tail = THREE_TURNS.slice(3 * perTurn - 3).map((row) => ({ ...row, seq: null }));
    expect(window.appendLive(tail)).toBe(3);
    expect(settled(window.turns())).toEqual(settled(wholeFold(THREE_TURNS).turns));
  });

  it("opens on the first page it is handed when nothing is loaded yet", () => {
    const window = new TranscriptWindow(adapter);
    expect(window.prependOlder(THREE_TURNS).folded).toBe(THREE_TURNS.length);
    expect(window.segmentCount).toBe(1);
    expect(settled(window.turns())).toEqual(settled(wholeFold(THREE_TURNS).turns));
  });

  it("merges a row stamped below what it holds back into the server's order", () => {
    // The socket window landed first and holds every machine row of turns 2
    // and 3; the tail page then brings the person's messages, which ride the
    // relay lane and were never in the window. Appended after, they would read
    // below the answers to them.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    const tail = THREE_TURNS.slice(perTurn);
    const isPrompt = (row: Row): boolean =>
      row.event.event_type === "message.created" && row.event.role === "user";
    expect(window.appendLive(tail.filter((row) => !isPrompt(row)))).toBe(2 * perTurn - 2);
    const merged = window.mergeLive(tail.filter(isPrompt));
    expect(merged).toEqual({ folded: 2, liveRefolded: true });
    expect(window.turns().map((turn) => turn.id)).toEqual(["u2", "a2", "u3", "a3"]);
    expect(settled(window.turns())).toEqual(settled(wholeFold(tail).turns));
    // A row already held is not folded twice, and one stamped above everything
    // appends without a re-fold.
    expect(window.mergeLive(tail.filter(isPrompt))).toEqual({ folded: 0, liveRefolded: false });
    const more = rows(turnEvents(4), tail[tail.length - 1].seq! + 1);
    expect(window.mergeLive(more)).toEqual({ folded: more.length, liveRefolded: false });
    expect(window.turns().map((turn) => turn.id)).toEqual(["u2", "a2", "u3", "a3", "u4", "a4"]);
  });
});

describe("what crosses a segment boundary", () => {
  it("settles a pending ask on an older page and keeps the live one answerable", () => {
    const old = turnEvents(1, { pendingAsk: true });
    const live = turnEvents(2, { pendingAsk: true });
    const all = rows([...old, ...live]);
    const window = new TranscriptWindow(adapter);
    window.appendLive(all.slice(old.length));
    window.prependOlder(all.slice(0, old.length));
    const asks = window
      .turns()
      .flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "permission")
      .map((part) => (part.kind === "permission" ? [part.requestId, part.status] : []));
    expect(asks).toEqual([
      ["req1", "resolved"],
      ["req2", "pending"],
    ]);
  });

  it("marks a tool still running on an older page as failed", () => {
    const old = turnEvents(1, { running: true });
    const live = turnEvents(2);
    const all = rows([...old, ...live]);
    const window = new TranscriptWindow(adapter);
    window.appendLive(all.slice(old.length));
    window.prependOlder(all.slice(0, old.length));
    const a1 = window.turns().find((turn) => turn.id === "a1");
    const tool = a1?.parts.find((part) => part.kind === "tool");
    expect(tool && tool.kind === "tool" ? tool.state : null).toBe("error");
    expect(a1?.status).toBe("cancelled");
  });

  it("dims the turns a compaction below elided, however far above they sit", () => {
    const compaction: HarnessEvent = {
      event_type: "compaction.applied",
      event_id: "cmp",
      part_id: "cmp",
      message_id: "a3",
      summarised_message_ids: ["u1", "a1"],
    };
    const all = rows([...turnEvents(1), ...turnEvents(2), ...turnEvents(3), compaction]);
    const perTurn = turnEvents(1).length;
    const window = new TranscriptWindow(adapter);
    window.appendLive(all.slice(2 * perTurn));
    window.prependOlder(all.slice(perTurn, 2 * perTurn));
    window.prependOlder(all.slice(0, perTurn));
    const cleared = window.turns().filter((turn) => turn.cleared === true).map((turn) => turn.id);
    expect(cleared).toEqual(["u1", "a1"]);
  });

  it("dims an already loaded page when the compaction arrives live", () => {
    const perTurn = turnEvents(1).length;
    const window = new TranscriptWindow(adapter);
    window.appendLive(THREE_TURNS.slice(2 * perTurn));
    window.prependOlder(THREE_TURNS.slice(0, 2 * perTurn));
    window.appendLive([
      {
        seq: null,
        eventId: "cmp",
        event: {
          event_type: "compaction.applied",
          event_id: "cmp",
          part_id: "cmp",
          message_id: "a3",
          summarised_message_ids: ["u2"],
        },
      },
    ]);
    expect(window.turns().find((turn) => turn.id === "u2")?.cleared).toBe(true);
    expect(window.turns().find((turn) => turn.id === "u1")?.cleared).toBeUndefined();
  });
});

describe("releasing the head", () => {
  it("drops whole head segments past the budget, never the live one, and lets them be re-read", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(THREE_TURNS.slice(2 * perTurn));
    window.prependOlder(THREE_TURNS.slice(perTurn, 2 * perTurn));
    window.prependOlder(THREE_TURNS.slice(0, perTurn));
    expect(window.releaseHead(perTurn * 2).released).toBe(perTurn);
    expect(window.oldestSeq).toBe(perTurn + 1);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u2", "a2", "u3", "a3"]);
    // Budget below one segment: everything but the live segment goes.
    expect(window.releaseHead(1).released).toBe(perTurn);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u3", "a3"]);
    // And the live segment holds one turn, which has no earlier turn start to
    // cut at, so nothing more goes however low the budget is.
    expect(window.releaseHead(1).released).toBe(0);
    // A released page is no longer "seen": scrolling up reads it again.
    expect(window.prependOlder(THREE_TURNS.slice(perTurn, 2 * perTurn)).folded).toBe(perTurn);
  });

  it("bounds a window that only ever grew at the live edge", () => {
    // A chat nobody scrolls back in has exactly one segment, so a budget that
    // can only drop whole head segments never applies to it — and the tab that
    // is left open across a multi-day turn is precisely that chat.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    const many = rows(Array.from({ length: 12 }, (_, i) => turnEvents(i + 1)).flat());
    window.appendLive(many);
    expect(window.segmentCount).toBe(1);
    expect(window.rowCount).toBe(12 * perTurn);

    const { released } = window.releaseHead(4 * perTurn);
    expect(released).toBeGreaterThan(0);
    expect(window.rowCount).toBeLessThanOrEqual(4 * perTurn);
    // What is left starts at a turn, and starts where the released rows stopped.
    expect(window.turns()[0].id).toBe(`u${13 - window.rowCount / perTurn}`);
    expect(window.oldestSeq).toBe(many.length - window.rowCount + 1);
    // And what went is fetchable again, joining as its own segment above.
    const back = window.prependOlder(many.slice(many.length - window.rowCount - perTurn, many.length - window.rowCount));
    expect(back.folded).toBe(perTurn);
    expect(window.segmentCount).toBe(2);
  });

  it("cuts the whole stamped prefix and still names what went", () => {
    // The shape both real sources actually produce: a stamped page read from
    // the durable record, then live rows the publisher sends with no sequence
    // of their own. A cut may take the whole stamped prefix because one past
    // its last row names every row that went and nothing that stayed.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    const stamped = rows([...turnEvents(1), ...turnEvents(2)]);
    const live = unstamped([...turnEvents(3), ...turnEvents(4)]);
    window.appendLive([...stamped, ...live]);

    expect(window.releaseHead(2 * perTurn).released).toBe(stamped.length);
    expect(window.rowCount).toBe(live.length);
    expect(window.turns().map((turn) => turn.id)).toEqual(["u3", "a3", "u4", "a4"]);
    // One past the last stamped row: the very next page brings back everything
    // that went, in order, rather than skipping its newest row.
    expect(window.oldestSeq).toBe(stamped.length + 1);
    const back = window.prependOlder(stamped.slice(perTurn));
    expect(back.folded).toBe(perTurn);
    expect(window.turns()[0].id).toBe("u2");
  });

  it("stops at the frontier rather than dropping a row it could not ask for again", () => {
    // Past the stamped prefix there is no cursor that names the dropped rows,
    // so the window stays over budget instead of losing transcript.
    const window = new TranscriptWindow(adapter);
    const stamped = rows(turnEvents(1));
    const live = unstamped([...turnEvents(2), ...turnEvents(3), ...turnEvents(4)]);
    window.appendLive([...stamped, ...live]);
    window.releaseHead(1);
    expect(window.rowCount).toBe(live.length);
    expect(window.oldestSeq).toBe(stamped.length + 1);
    // And it does not keep chipping away at what it cannot name.
    expect(window.releaseHead(1).released).toBe(0);
    expect(window.rowCount).toBe(live.length);
  });

  it("bounds a live lane its source stamps for it", () => {
    // The same window, driven the way a source that stamps its live rows from
    // `newestSeq` drives it: every row is addressable, so the budget holds.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(rows(turnEvents(1)));
    for (let n = 2; n <= 40; n += 1) {
      const next = window.newestSeq ?? 0;
      window.appendLive(rows(turnEvents(n), next + 1));
      window.releaseHead(4 * perTurn);
    }
    expect(window.rowCount).toBeLessThanOrEqual(4 * perTurn);
    expect(window.oldestSeq).not.toBeNull();
    expect(window.turns().map((turn) => turn.id)).toContain("u40");
  });

  it("lets go of rows above a streamed token, which names nothing a page could return", () => {
    // A token frame sits above the rows the turn settles into, so reading one
    // as the boundary the cut must stop at makes the budget a no-op on any
    // chat somebody is watching: the first token holds the window open.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    let seq = 1;
    for (let n = 1; n <= 20; n += 1) {
      window.appendLive([
        { seq: null, ephemeral: true, eventId: null, event: chunkEvent(n) },
      ]);
      window.appendLive(rows(turnEvents(n), seq));
      seq += perTurn;
      window.releaseHead(4 * perTurn);
    }
    expect(window.rowCount).toBeLessThanOrEqual(4 * perTurn + 4);
    expect(window.oldestSeq).not.toBeNull();
    expect(window.turns().map((turn) => turn.id)).toContain("u20");
  });

  it("folds the stamped rows that follow a live segment a token frame opened", () => {
    // The segment's floor is the lowest sequence it holds, and a segment
    // holding only a token holds none. Read as a floor, `Infinity` rejects
    // every stamped row that arrives after it — the whole turn, silently.
    const window = new TranscriptWindow(adapter);
    window.appendLive([{ seq: null, ephemeral: true, eventId: null, event: chunkEvent(1) }]);
    expect(window.oldestSeq).toBeNull();
    expect(window.appendLive(rows(turnEvents(1)))).toBe(turnEvents(1).length);
    expect(window.oldestSeq).toBe(1);
    expect(window.turns().map((turn) => turn.id)).toContain("u1");
  });

  it("keeps ONE turn larger than the budget whole, however it is stamped", () => {
    // The ceiling the cut leaves, pinned rather than left to be discovered: a
    // cut lands only on a turn start, so a single turn over the budget sheds
    // nothing. Re-folding from inside a turn would attach its remaining parts
    // to a synthetic turn with no question above it, and the page that brought
    // the opening back would fold the same message under a second turn id —
    // worse, for the reader, than a window temporarily over its budget.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    window.appendLive(rows(turnEvents(1)));
    // One long turn: every row stamped, so nothing but the turn boundary is
    // holding the cut back.
    for (let part = 0; part < 12 * perTurn; part += 1) {
      const next = window.newestSeq ?? 0;
      window.appendLive(
        rows(
          [
            {
              event_type: "part.created",
              event_id: `a1-p${part}`,
              message_id: "a1",
              part: { part_id: `a1-t${part}`, message_id: "a1", type: "text", text: `word ${part}` },
            },
          ],
          next + 1,
        ),
      );
      window.releaseHead(4 * perTurn);
    }
    expect(window.rowCount).toBeGreaterThan(4 * perTurn);
    expect(window.segmentCount).toBe(1);
    expect(window.turns().map((turn) => turn.id)).toContain("u1");
    // And the moment a second turn starts, the whole of the first is a legal
    // cut again — the ceiling is per turn, not for the life of the tab.
    const next = window.newestSeq ?? 0;
    window.appendLive(rows(turnEvents(2), next + 1));
    window.releaseHead(4 * perTurn);
    expect(window.turns().map((turn) => turn.id)).not.toContain("u1");
    expect(window.turns().map((turn) => turn.id)).toContain("u2");
  });

  it("never releases a row the durable record has not stamped", () => {
    // A live frame with no sequence cannot be asked for again, so releasing it
    // would delete transcript rather than page it out.
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    const stamped = rows([...turnEvents(1), ...turnEvents(2)]);
    const unstamped: Row[] = rows([...turnEvents(3), ...turnEvents(4)], stamped.length + 1).map((row) => ({
      ...row,
      seq: null,
    }));
    window.appendLive([...stamped, ...unstamped]);
    window.releaseHead(perTurn);
    expect(window.rowCount).toBeGreaterThanOrEqual(2 * perTurn);
    expect(window.turns().map((turn) => turn.id)).toContain("u3");
    expect(window.turns().map((turn) => turn.id)).toContain("u4");
  });

  it("keeps a summarised turn dimmed across a live-edge release", () => {
    const window = new TranscriptWindow(adapter);
    const perTurn = turnEvents(1).length;
    const four = rows([...turnEvents(1), ...turnEvents(2), ...turnEvents(3), ...turnEvents(4)]);
    window.appendLive(four);
    window.appendLive([
      {
        seq: four.length + 1,
        eventId: "c1",
        event: {
          event_type: "compaction.applied",
          event_id: "c1",
          summarised_message_ids: ["u3"],
        } as HarnessEvent,
      },
    ]);
    expect(window.turns().find((turn) => turn.id === "u3")?.cleared).toBe(true);
    window.releaseHead(2 * perTurn + 1);
    expect(window.turns().find((turn) => turn.id === "u1")).toBeUndefined();
    expect(window.turns().find((turn) => turn.id === "u3")?.cleared).toBe(true);
  });

  it("forgets everything on reset", () => {
    const window = new TranscriptWindow(adapter);
    window.appendLive(THREE_TURNS);
    window.reset();
    expect(window.turns()).toEqual([]);
    expect(window.oldestSeq).toBeNull();
    expect(window.appendLive(THREE_TURNS)).toBe(THREE_TURNS.length);
  });
});
