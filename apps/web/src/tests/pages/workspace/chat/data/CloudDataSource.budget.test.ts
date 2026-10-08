// What a tab left open on a cloud chat for days holds in memory.
//
// The loaded window may only let go of rows it can ask the durable record for
// again, so the budget reaches exactly as far as the sequences it has been
// told. The server stamps every live append with the sequence its durable
// write assigned, which is what makes the live lane releasable; the same loop
// against frames carrying no sequence is the shape that grew forever, and is
// driven here as the control.

import { describe, expect, it, vi } from "vitest";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { TRANSCRIPT_WINDOW_MAX_ROWS } from "@/pages/workspace/chat/data/transcriptWindow";

const CHAT = "chat-long-lived";

/** Rows in the page a long chat opens on — one REST page, as the server sends it. */
const HISTORY_ROWS = 200;
/** Rows one agent turn publishes: the person's message and its text, then the
 *  machine's answer and the tool calls it made getting there. */
const ROWS_PER_TURN = 40;
/** Live turns driven through the window. Enough that the live lane alone is
 *  twice the budget, so a window that can only shed its opening page is
 *  unmistakably over it. */
const LIVE_TURNS = (2 * TRANSCRIPT_WINDOW_MAX_ROWS) / ROWS_PER_TURN;

interface Row {
  id: string;
  chat_id: string;
  seq: number;
  role: string;
  kind: string;
  event_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

const AT = "2026-01-01T00:00:00.000Z";

/** The page the chat opens on: complete turns of `ROWS_PER_TURN` rows, the
 *  first row of each a person's message so the window has turn starts to cut
 *  at. */
const HISTORY: Row[] = Array.from({ length: HISTORY_ROWS }, (_, index) => {
  const seq = index + 1;
  const offset = index % ROWS_PER_TURN;
  const turn = Math.floor(index / ROWS_PER_TURN) + 1;
  const isPrompt = offset === 0;
  return {
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role: isPrompt ? "user" : "assistant",
    kind: isPrompt ? "message.created" : "part.created",
    event_id: isPrompt ? `h${turn}` : `h${turn}-${offset}`,
    payload: isPrompt
      ? {
          event_type: "message.created",
          event_id: `h${turn}`,
          message_id: `h${turn}`,
          role: "user",
          time: AT,
        }
      : {
          event_type: "part.created",
          event_id: `h${turn}-${offset}`,
          message_id: `h${turn}`,
          part: {
            part_id: `h${turn}-${offset}-text`,
            message_id: `h${turn}`,
            type: "text",
            text: `history ${turn}.${offset}`,
          },
        },
    created_at: AT,
  };
});

/** One live turn's entries as the publisher sends them, optionally stamped the
 *  way the server stamps an accepted append. */
function liveTurn(n: number, opts: { from: number | null }): Record<string, unknown>[] {
  const stamp = (offset: number): Record<string, unknown> =>
    opts.from === null ? {} : { seq: opts.from + offset };
  const entries: Record<string, unknown>[] = [
    {
      event_id: `u${n}`,
      event_type: "message.created",
      message_id: `u${n}`,
      role: "user",
      time: AT,
      ...stamp(0),
    },
  ];
  for (let part = 1; part < ROWS_PER_TURN; part += 1) {
    entries.push({
      event_id: `u${n}-${part}`,
      event_type: "part.created",
      message_id: `u${n}`,
      part: {
        part_id: `u${n}-${part}-text`,
        message_id: `u${n}`,
        type: "text",
        text: `turn ${n} part ${part}`,
      },
      ...stamp(part),
    });
  }
  return entries;
}

function server() {
  return vi.fn(
    async (_chatId: string, opts: { afterSeq?: number; before?: number; tail?: boolean; limit?: number }) => {
      if (opts.tail) {
        return {
          items: HISTORY,
          next_after_seq: HISTORY[HISTORY.length - 1].seq,
          resync_from: null,
          prev_before: HISTORY[0].seq,
          has_older: true,
        };
      }
      if (opts.before !== undefined) {
        const below = HISTORY.filter((row) => row.seq < opts.before!);
        return {
          items: below,
          next_after_seq: below[below.length - 1]?.seq ?? 0,
          resync_from: null,
          prev_before: below[0]?.seq ?? null,
          has_older: below.length > 0 && below[0].seq > 1,
        };
      }
      const after = opts.afterSeq ?? 0;
      const items = HISTORY.filter((row) => row.seq > after);
      return { items, next_after_seq: items[items.length - 1]?.seq ?? after, resync_from: null };
    },
  );
}

function doc(): {
  handle: DocHandle<never>;
  append: (events: unknown[]) => void;
  chunk: (events: unknown[]) => void;
} {
  let listener: ((message: unknown) => void) | null = null;
  const handle = {
    onMessage: (fn: (message: unknown) => void) => {
      listener = fn;
      return () => undefined;
    },
    onPhase: () => () => undefined,
    getPhase: () => ({ phase: "live", epoch: 1, seq: 0, peerId: "p:1", canWrite: false, pending: 0, error: null }),
    sendOp: () => Promise.reject(new Error("a reader never writes")),
    dispose: () => undefined,
  } as unknown as DocHandle<never>;
  return {
    handle,
    append: (events) =>
      listener?.({ kind: "op", ephemeral: false, payload: { intent: "append", events } }),
    chunk: (events) =>
      listener?.({ kind: "op", ephemeral: true, payload: { intent: "chunk", events } }),
  };
}

/** The reader opens the chat on its newest page and then sits at the live
 *  edge while `LIVE_TURNS` turns arrive, weighing the budget as the surface
 *  does — on every commit that grew the tape. With `streaming`, each turn is
 *  preceded by a token frame on the ephemeral lane, which is what a reader
 *  watching a live chat actually receives. Returns the turns still loaded. */
async function readerLeftOpen(opts: {
  stamped: boolean;
  streaming?: boolean;
}): Promise<{ turns: number; newest: boolean; oldestTurn: number }> {
  const listMessages = server();
  const { handle, append, chunk } = doc();
  const src = new CloudDataSource({
    rest: { listMessages } as never,
    openDoc: () => handle,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
  src.subscribeChat(CHAT, () => undefined);
  await src.getChatTurns(CHAT);
  for (let n = 1; n <= LIVE_TURNS; n += 1) {
    if (opts.streaming) {
      chunk([
        { event_type: "agent.message_chunk", message_id: `u${n}`, part_id: `u${n}-s`, delta: "…" },
      ]);
    }
    const from = opts.stamped ? HISTORY_ROWS + (n - 1) * ROWS_PER_TURN + 1 : null;
    append(liveTurn(n, { from }));
    src.releaseOlderTurns(CHAT);
  }
  const turns = await src.getChatTurns(CHAT);
  const held = turns
    .map((turn) => /^u(\d+)$/.exec(turn.id)?.[1])
    .filter((n): n is string => n !== undefined)
    .map(Number);
  return {
    turns: turns.length,
    newest: turns.some((turn) => turn.id === `u${LIVE_TURNS}`),
    // Which live turn the window still reaches back to. The reader who never
    // scrolls should keep the newest and have let go of the rest.
    oldestTurn: held.length > 0 ? Math.min(...held) : 0,
  };
}

describe("a tab left open on a long cloud chat", () => {
  it("stays inside the row budget once the server stamps its live appends", async () => {
    const { turns, newest } = await readerLeftOpen({ stamped: true });
    // Every turn here is `ROWS_PER_TURN` rows, and the window cuts at a turn
    // start, so the budget in rows reads as a budget in turns — plus the one
    // partial turn a cut may leave above the boundary.
    const budgetInTurns = Math.ceil(TRANSCRIPT_WINDOW_MAX_ROWS / ROWS_PER_TURN) + 1;
    expect(turns).toBeLessThanOrEqual(budgetInTurns);
    expect(newest).toBe(true);
  });

  it("stays inside the budget while the machine is streaming into it", async () => {
    // The state a reader is actually in: a token frame lands above the rows
    // each turn settles into. Those frames are never in the durable record,
    // so they are not a boundary the cut has to stop at — read as one, a
    // single streamed token held the whole window open for the rest of the
    // tab's life even with every durable row stamped.
    const { newest, oldestTurn } = await readerLeftOpen({ stamped: true, streaming: true });
    // A turn's rows plus its token frame, so the budget reaches back this far
    // and no further. Measured on the turn the window still opens on, because
    // each token also folds a turn of its own and the count alone would hide
    // a window twice the size it should be.
    const reach = Math.ceil(TRANSCRIPT_WINDOW_MAX_ROWS / (ROWS_PER_TURN + 1)) + 1;
    expect(oldestTurn).toBeGreaterThanOrEqual(LIVE_TURNS - reach);
    expect(newest).toBe(true);
  });

  it("grows for the life of the tab when the live appends carry no sequence", async () => {
    // The control: the same reader, the same turns, the same release on every
    // commit — and nothing above the opening page can be let go, because no
    // row above it can be asked for again. This is what the stamp fixes, and
    // what would come back if it were removed.
    const { turns, newest } = await readerLeftOpen({ stamped: false });
    const budgetInTurns = Math.ceil(TRANSCRIPT_WINDOW_MAX_ROWS / ROWS_PER_TURN) + 1;
    expect(turns).toBeGreaterThan(budgetInTurns);
    // Not one live turn ever went: the page the chat opened on is the whole
    // rebate, and after it the window grows one row per row for as long as the
    // tab is open.
    expect(turns).toBeGreaterThanOrEqual(LIVE_TURNS);
    expect(newest).toBe(true);
  });
});
