// A long cloud chat opens on its newest page and reads the pages above it on
// demand — from the durable record, joined with the socket's window so nothing
// is shown twice or out of order.

import { describe, expect, it, vi } from "vitest";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import type { ChatEvent } from "@/pages/workspace/chat/data/model";

const CHAT = "chat-long";

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

/** Rows per turn: the person's prompt row, the machine's echo of it (the
 *  message and the text part carrying the same words — the part is what the
 *  fold merges the prompt on), and one answer. */
const ROWS_PER_TURN = 4;
const TURNS = 6;
const startOf = (n: number): number => (n - 1) * ROWS_PER_TURN + 1;
const lastSeq = TURNS * ROWS_PER_TURN;

function turn(n: number, seq: number): Row[] {
  const at = `2026-01-01T00:00:${String(n).padStart(2, "0")}.000Z`;
  const row = (offset: number, role: string, kind: string, event_id: string, payload: Record<string, unknown>): Row => ({
    id: `row-${seq + offset}`,
    chat_id: CHAT,
    seq: seq + offset,
    role,
    kind,
    event_id,
    payload,
    created_at: at,
  });
  return [
    row(0, "user", "prompt", `usr:c${n}`, { text: `ask ${n}`, client_id: `c${n}` }),
    row(1, "user", "message.created", `u${n}`, {
      event_id: `u${n}`,
      role: "user",
      kind: "message.created",
      payload: { event_type: "message.created", message_id: `u${n}`, role: "user", time: at },
    }),
    row(2, "user", "part.created", `u${n}-p`, {
      event_id: `u${n}-p`,
      role: "user",
      kind: "part.created",
      payload: {
        event_type: "part.created",
        message_id: `u${n}`,
        part: { part_id: `u${n}-text`, message_id: `u${n}`, type: "text", text: `ask ${n}` },
      },
    }),
    row(3, "assistant", "message.completed", `a${n}`, {
      event_id: `a${n}`,
      role: "assistant",
      kind: "message.completed",
      payload: {
        event_type: "part.created",
        message_id: `a${n}`,
        part: { part_id: `a${n}-text`, message_id: `a${n}`, type: "text", text: `answer ${n}` },
      },
    }),
  ];
}

const ALL: Row[] = [];
for (let n = 1; n <= TURNS; n += 1) ALL.push(...turn(n, startOf(n)));

/** A server over `ALL`: the same backward page rules the real one applies,
 *  with a page of `limit` rows aligned down to a prompt row within reach. */
function server(limit = ROWS_PER_TURN) {
  return vi.fn(async (_chatId: string, opts: { afterSeq?: number; before?: number; tail?: boolean; limit?: number }) => {
    if (opts.tail || opts.before !== undefined) {
      const below = opts.before === undefined ? ALL : ALL.filter((row) => row.seq < opts.before!);
      let page = below.slice(-limit);
      if (page.length > 0 && page[0].kind !== "prompt") {
        const low = page[0].seq;
        const anchor = [...below].reverse().find((row) => row.kind === "prompt" && row.seq < low && row.seq >= low - limit);
        if (anchor) page = below.filter((row) => row.seq >= anchor.seq);
      }
      const lowest = page[0]?.seq ?? null;
      return {
        items: page,
        next_after_seq: page[page.length - 1]?.seq ?? 0,
        resync_from: null,
        prev_before: lowest,
        has_older: lowest !== null && lowest > 1,
      };
    }
    const after = opts.afterSeq ?? 0;
    const items = ALL.filter((row) => row.seq > after).slice(0, limit);
    return { items, next_after_seq: items[items.length - 1]?.seq ?? after, resync_from: null };
  });
}

function doc(): {
  handle: DocHandle<never>;
  snapshot: (events: unknown[]) => void;
  append: (events: unknown[]) => void;
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
    snapshot: (events) => listener?.({ kind: "snapshot", state: { meta: {}, events, ids: {} } }),
    // A live append op as the publisher sent it, before the server stamped the
    // sequence its durable write assigned. These cases are about how a page
    // and the socket's window join, which is the same either way; the stamp
    // and what it buys are driven in `CloudDataSource.budget.test.ts`.
    append: (events) =>
      listener?.({ kind: "op", ephemeral: false, payload: { intent: "append", events } }),
  };
}

function source(listMessages: ReturnType<typeof server>, handle?: DocHandle<never>) {
  return new CloudDataSource({
    rest: { listMessages } as never,
    openDoc: () => handle ?? doc().handle,
    acquire: () => () => undefined,
    clientId: () => "client-1",
  });
}

function said(turns: { parts: { kind: string; text?: string }[] }[]): string[] {
  return turns.flatMap((t) => t.parts.filter((p) => p.kind === "text").map((p) => p.text ?? ""));
}

describe("opening on the newest page", () => {
  it("reads the tail, not the transcript from sequence zero, and says there is more above", async () => {
    const listMessages = server();
    const src = source(listMessages);
    const turns = await src.getChatTurns(CHAT);
    expect(listMessages.mock.calls[0][1]).toMatchObject({ tail: true });
    expect(listMessages.mock.calls.some(([, opts]) => opts.afterSeq === 0)).toBe(false);
    expect(said(turns)).toEqual(["ask 6", "answer 6"]);
    expect(src.transcriptHistory(CHAT)).toEqual({ hasOlder: true, loading: false });
  });

  it("continues the live tail forward from the newest page's highest sequence", async () => {
    const listMessages = server();
    const src = source(listMessages);
    await src.getChatTurns(CHAT);
    const forward = listMessages.mock.calls.filter(([, opts]) => opts.afterSeq !== undefined);
    expect(forward.map(([, opts]) => opts.afterSeq)).toEqual([lastSeq]);
  });
});

describe("scrolling up", () => {
  it("reads the page below the window and prepends it, in order, with nothing twice", async () => {
    const listMessages = server();
    const src = source(listMessages);
    const events: ChatEvent[] = [];
    src.subscribeChat(CHAT, (event) => events.push(event));
    await src.getChatTurns(CHAT);
    await src.loadOlderTurns(CHAT);
    expect(listMessages.mock.calls.at(-1)?.[1]).toMatchObject({ before: startOf(6) });
    expect(said(await src.getChatTurns(CHAT))).toEqual(["ask 5", "answer 5", "ask 6", "answer 6"]);
    expect(events.at(-1)?.kind).toBe("history_loaded");
    expect(src.transcriptHistory(CHAT).hasOlder).toBe(true);
    // The whole way up: every turn once, then nothing older.
    while (src.transcriptHistory(CHAT).hasOlder) await src.loadOlderTurns(CHAT);
    expect(said(await src.getChatTurns(CHAT))).toEqual(
      [1, 2, 3, 4, 5, 6].flatMap((n) => [`ask ${n}`, `answer ${n}`]),
    );
    expect(events.filter((event) => event.kind === "history_loaded")).toHaveLength(5);
    // Asking again with nothing older reads nothing.
    const calls = listMessages.mock.calls.length;
    await src.loadOlderTurns(CHAT);
    expect(listMessages.mock.calls.length).toBe(calls);
  });

  it("asks for one page at a time", async () => {
    const listMessages = server();
    const src = source(listMessages);
    await src.getChatTurns(CHAT);
    const calls = listMessages.mock.calls.length;
    await Promise.all([src.loadOlderTurns(CHAT), src.loadOlderTurns(CHAT), src.loadOlderTurns(CHAT)]);
    expect(listMessages.mock.calls.length).toBe(calls + 1);
  });
});

describe("the live tail while a page is in flight", () => {
  it("folds the arriving turn into the newest page, and the page still joins above it", async () => {
    // A `before` read the test holds open, so the live append lands while the
    // page above is still on the wire.
    const gate: { open: (() => void) | null } = { open: null };
    const base = server();
    const listMessages = vi.fn(async (chatId: string, opts: Parameters<typeof base>[1]) => {
      const answer = await base(chatId, opts);
      if (opts.before !== undefined) {
        await new Promise<void>((resolve) => {
          gate.open = resolve;
        });
      }
      return answer;
    });
    const { handle, append } = doc();
    const src = source(listMessages as unknown as ReturnType<typeof server>, handle);
    src.subscribeChat(CHAT, () => undefined);
    await src.getChatTurns(CHAT);

    const pending = src.loadOlderTurns(CHAT);
    await vi.waitFor(() => expect(gate.open).not.toBeNull());
    // Turn 7 arrives on the socket while the reader waits for turn 5.
    append([
      { event_id: "u7", event_type: "message.created", message_id: "u7", role: "user" },
      {
        event_id: "u7p",
        event_type: "part.created",
        message_id: "u7",
        part: { part_id: "u7t", message_id: "u7", type: "text", text: "ask 7" },
      },
    ]);
    expect(said(await src.getChatTurns(CHAT))).toEqual(["ask 6", "answer 6", "ask 7"]);

    gate.open?.();
    await pending;
    expect(said(await src.getChatTurns(CHAT))).toEqual([
      "ask 5",
      "answer 5",
      "ask 6",
      "answer 6",
      "ask 7",
    ]);
  });
});

describe("the socket's window and the page", () => {
  it("drops snapshot entries below the newest page and folds the rest once", async () => {
    const listMessages = server();
    const { handle, snapshot } = doc();
    const src = source(listMessages, handle);
    src.subscribeChat(CHAT, () => undefined);
    await src.getChatTurns(CHAT);
    // A retained window reaching back to turn 4: its entries carry the
    // sequence the server stamped. Those below the page are an older page's.
    snapshot(
      ALL.slice(startOf(4) - 1)
        .filter((row) => row.kind !== "prompt")
        .map((row) => ({ ...row.payload, seq: row.seq })),
    );
    expect(said(await src.getChatTurns(CHAT))).toEqual(["ask 6", "answer 6"]);
    await src.loadOlderTurns(CHAT);
    expect(said(await src.getChatTurns(CHAT))).toEqual(["ask 5", "answer 5", "ask 6", "answer 6"]);
  });

  it("opens on the snapshot when it wins the race, and the page below still joins whole", async () => {
    const listMessages = server();
    const { handle, snapshot } = doc();
    const src = source(listMessages, handle);
    src.subscribeChat(CHAT, () => undefined);
    // The snapshot lands before any durable read: turn 6's echo and answer.
    snapshot(ALL.slice(startOf(6)).map((row) => ({ ...row.payload, seq: row.seq })));
    const turns = await src.getChatTurns(CHAT);
    expect(said(turns)).toEqual(["ask 6", "answer 6"]);
    expect(turns.filter((t) => t.author === "user")).toHaveLength(1);
    await src.loadOlderTurns(CHAT);
    expect(said(await src.getChatTurns(CHAT))).toEqual(["ask 5", "answer 5", "ask 6", "answer 6"]);
  });
});
