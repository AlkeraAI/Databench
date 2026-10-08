// A refused forward read is resumed, never swallowed.
//
// The rows that reach a live tab ONLY by reading the durable record — a
// person's own prompt (it rides the `user_message` relay, never the socket's
// window), the answers the server records for an ask, the `Stopped by` notes —
// are folded by one path: the catch-up the source runs after a relay and after
// every socket snapshot. Neither caller can hold a rejection, so a 429 on the
// second of three pages must not leave the window PARTLY folded with nothing
// scheduled to try again: inside a long streaming turn there may be neither
// another relay nor another snapshot.
//
// What must hold, and what each test below pins:
//
//  - folding a row twice is a no-op, which is what makes a resume safe;
//  - a read cut short records the pages it DID fold, so the resume reads on
//    from there and the end state equals one clean read of the same log;
//  - a refusal is retried on a bounded ladder that honours `Retry-After`;
//  - a ladder that runs out stops claiming the chat is current and says so the
//    way a reconnect does, instead of leaving a silently stale tape;
//  - a catch-up already in flight or already armed is joined, not duplicated.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";
import { CATCH_UP_RETRY_ATTEMPTS, CATCH_UP_RETRY_CAP_MS, RETRY_JITTER_RATIO } from "@/lib/limits";
import type { ChatEvent } from "@/pages/workspace/chat/data/model";
import type { DocHandle, DocMessage } from "@/api/realtime/docSync";

/** A durable row exactly as the messages route answers one: the envelope the
 *  server sequences by, with the harness event one level in. */
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

function row(seq: number, role: string, kind: string, inner: Record<string, unknown>): Row {
  const id = `e${seq}`;
  return {
    id: `row-${seq}`,
    chat_id: "c1",
    seq,
    role,
    kind,
    event_id: id,
    payload: { event_id: id, role, kind, payload: inner },
    created_at: "2026-01-01T00:00:00.000Z",
  };
}

/** A person's prompt and the assistant message answering it. Everything the box
 *  streams afterwards is one more text part, which is what makes the transcript
 *  a string a test can compare two readings of. */
function opening(): Row[] {
  return [
    row(1, "user", "message.created", {
      event_type: "message.created",
      message_id: "u1",
      role: "user",
    }),
    row(2, "user", "part.created", {
      event_type: "part.created",
      message_id: "u1",
      part: { part_id: "u1-text", message_id: "u1", type: "text", text: "what changed?" },
    }),
    row(3, "assistant", "message.created", {
      event_type: "message.created",
      message_id: "a1",
      role: "assistant",
    }),
  ];
}

/** One more thing the box said, at sequence `seq`. */
function said(seq: number): Row {
  return row(seq, "assistant", "part.created", {
    event_type: "part.created",
    message_id: "a1",
    part: { part_id: `a1-t${seq}`, message_id: "a1", type: "text", text: `line ${seq}` },
  });
}

type Page = { items: Row[]; next_after_seq: number; resync_from: null; has_older: boolean };

/** A refusal shaped exactly as the transport mints one: `ApiError` off a 429,
 *  carrying whatever `Retry-After` the response held. */
function refusal(retryAfter?: string): ApiError {
  const headers = new Headers(retryAfter === undefined ? {} : { "retry-after": retryAfter });
  return new ApiError(429, { error: { code: "rate_limited", message: "Slow down." } }, "x", headers);
}

/** An answer that is not a refusal to re-ask but a statement about the chat. */
function settled(status: number): ApiError {
  return new ApiError(status, { error: { code: "not_found", message: "No such chat." } }, "x");
}

/** The server's `list_messages`, paged `pageRows` at a time, over a log that can
 *  grow the way a live one does, with a script of refusals keyed by the
 *  `after_seq` the reader asks from. A scripted refusal is consumed on use, so
 *  "refuse the page from 8 once" is exactly one refusal. */
function record(pageRows: number) {
  const log: Row[] = opening();
  const fail = new Map<number, ApiError[]>();
  const reads: (number | "tail")[] = [];
  const rest = {
    listMessages: (
      _chatId: string,
      q: { afterSeq?: number; before?: number; tail?: boolean },
    ): Promise<Page> => {
      if (q.before !== undefined) {
        // The page ABOVE a sequence, exclusive — what a reader scrolling up
        // asks for, and the only way rows above a rebuilt window come back.
        const above = log.filter((r) => r.seq < (q.before as number));
        const items = above.slice(Math.max(above.length - pageRows, 0));
        return Promise.resolve({
          items,
          next_after_seq: items.length === 0 ? 0 : items[items.length - 1].seq,
          resync_from: null,
          has_older: above.length > items.length,
        });
      }
      if (q.tail) {
        reads.push("tail");
        const items = log.slice(Math.max(log.length - pageRows, 0));
        return Promise.resolve({
          items,
          next_after_seq: items.length === 0 ? 0 : items[items.length - 1].seq,
          resync_from: null,
          has_older: log.length > items.length,
        });
      }
      const after = q.afterSeq ?? 0;
      reads.push(after);
      const queued = fail.get(after);
      if (queued !== undefined && queued.length > 0) return Promise.reject(queued.shift());
      const items = log.filter((row) => row.seq > after).slice(0, pageRows);
      return Promise.resolve({
        items,
        next_after_seq: items.length === 0 ? after : items[items.length - 1].seq,
        resync_from: null,
        has_older: false,
      });
    },
  };
  return {
    rest,
    reads,
    /** The box said `count` more things. */
    grow(count: number): void {
      const from = log[log.length - 1].seq;
      for (let i = 1; i <= count; i += 1) log.push(said(from + i));
    },
    refuseAt(after: number, error: ApiError, times = 1): void {
      fail.set(
        after,
        Array.from({ length: times }, () => error),
      );
    },
    forward: (): number[] => reads.filter((r): r is number => r !== "tail"),
    tails: (): number => reads.filter((r) => r === "tail").length,
  };
}

/** The chat document. Each `openDoc` hands back a fresh handle over the same
 *  wire — what a reconnect is — and `snapshot()` is the frame that arrives. */
function wire() {
  const listeners = new Set<(m: DocMessage<unknown>) => void>();
  return {
    open: (): DocHandle<unknown> => {
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
          phase: "live" as const,
          epoch: 1,
          seq: 0,
          peerId: "p:1",
          canWrite: false,
          pending: 0,
          error: null,
        }),
        sendOp: () => Promise.reject(new Error("a reader never writes")),
        dispose: () => mine.forEach((l) => listeners.delete(l)),
      };
    },
    /** A re-hello: the frame every reconnect delivers, and the second of the
     *  two doors a catch-up comes through. Its retained window is empty here,
     *  so the ONLY thing that can close the hole is the durable read. */
    snapshot(): void {
      listeners.forEach((l) => l({ kind: "snapshot", state: { events: [] }, epoch: 1, seq: 0 }));
    },
  };
}

function scene(pageRows = 2, random: () => number = () => 1) {
  const server = record(pageRows);
  const document = wire();
  const events: ChatEvent[] = [];
  const source = new CloudDataSource({
    rest: server.rest as never,
    openDoc: () => document.open() as DocHandle<never>,
    acquire: () => () => undefined,
    clientId: () => "client",
    pageRows,
    random,
  });
  source.subscribeChat("c1", (event) => events.push(event));
  return { server, document, source, events };
}

type Part = { kind: string; text?: string };
type Turn = { author: string; parts: Part[] };

/** What the reader can see, flattened to a string. Two readings of one log must
 *  agree here or they are not the same chat. */
async function tape(source: CloudDataSource): Promise<string> {
  const turns = (await source.getChatTurns("c1")) as unknown as Turn[];
  return turns
    .map((t) => `${t.author}: ${t.parts.map((p) => p.text ?? p.kind).join(" | ")}`)
    .join("\n");
}

/** Let every already-resolved promise run, without moving the clock. */
async function settle(times = 12): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}

/** Page up until the durable record holds nothing above the window — what a
 *  reader scrolling to the top of a rebuilt tape does. */
async function readToTop(source: CloudDataSource): Promise<void> {
  for (let page = 0; page < 50; page += 1) {
    if (!source.transcriptHistory("c1").hasOlder) return;
    if (!(await source.loadOlderTurns("c1"))) return;
  }
  throw new Error("the history never ran out");
}

/** The chat as it stands when the box has been streaming for a while: opened,
 *  read once, and `grown` rows further on than the cursor the open left. */
async function midTurn(s: ReturnType<typeof scene>, grown: number): Promise<void> {
  await s.source.getChatTurns("c1");
  s.server.grow(grown);
}

describe("the fold is idempotent, which is what lets a cut-short read resume", () => {
  it("folds rows it has already folded into nothing", async () => {
    const s = scene(50);
    await midTurn(s, 6);
    s.document.snapshot();
    await settle();
    const once = await tape(s.source);
    // The same rows again, through the same door a resumed read uses.
    s.document.snapshot();
    await settle();
    expect(await tape(s.source)).toBe(once);
  });
});

describe("a forward read refused halfway", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resumes and ends where one clean read of the same log ends", async () => {
    // The reference: the same log, the same reads, never refused.
    const clean = scene();
    await midTurn(clean, 6);
    clean.document.snapshot();
    await settle();
    const expected = await tape(clean.source);
    expect(clean.server.forward()).toEqual([3, 3, 5, 7, 9]);

    const s = scene();
    await midTurn(s, 6);
    // The second forward page is refused once.
    s.server.refuseAt(5, refusal());
    s.document.snapshot();
    await settle();
    // Nothing else triggers a read — no relay, no second snapshot — so only the
    // retry can close the hole. Before it, the tape really is short.
    expect(await tape(s.source)).not.toBe(expected);

    await vi.advanceTimersByTimeAsync(CATCH_UP_RETRY_CAP_MS);
    await settle();
    expect(await tape(s.source)).toBe(expected);
    // And it read ON from the page that failed rather than from the top: the
    // clean reads above, with 5 asked for exactly twice — once refused, once
    // answered — and nothing below 5 asked for again.
    expect(s.server.forward()).toEqual([3, 3, 5, 5, 7, 9]);
  });

  it("waits the Retry-After the refusal named, and never less", async () => {
    // The bottom of the jitter band: the wait is exactly what the server asked.
    const s = scene(2, () => 0);
    await midTurn(s, 6);
    s.server.refuseAt(5, refusal("4"));
    s.document.snapshot();
    await settle();
    const before = s.server.forward().length;

    await vi.advanceTimersByTimeAsync(3_900);
    await settle();
    expect(s.server.forward().length).toBe(before);

    await vi.advanceTimersByTimeAsync(200);
    await settle();
    expect(s.server.forward().length).toBeGreaterThan(before);
  });

  it("spreads the wait, so every tab refused in one second does not re-ask in one", async () => {
    // The top of the band. The jitter is ADDED to the server's word and never
    // taken off it, so 4 s becomes 4 s plus the ratio and never 4 s minus it.
    const s = scene(2, () => 1);
    await midTurn(s, 6);
    s.server.refuseAt(5, refusal("4"));
    s.document.snapshot();
    await settle();
    const before = s.server.forward().length;

    await vi.advanceTimersByTimeAsync(4_100);
    await settle();
    expect(s.server.forward().length).toBe(before);

    await vi.advanceTimersByTimeAsync(4_000 * RETRY_JITTER_RATIO);
    await settle();
    expect(s.server.forward().length).toBeGreaterThan(before);
  });

  it("does not climb the limiter's ladder for an answer that will not change", async () => {
    // A chat that was deleted, or a reader whose access went away. Asking eight
    // more times cannot make the server say anything else.
    const gone = scene();
    await midTurn(gone, 6);
    gone.server.refuseAt(5, settled(404), CATCH_UP_RETRY_ATTEMPTS + 3);
    gone.document.snapshot();
    await settle();
    await vi.advanceTimersByTimeAsync(CATCH_UP_RETRY_CAP_MS * CATCH_UP_RETRY_ATTEMPTS * 2);
    await settle();
    expect(gone.server.forward().filter((a) => a === 5).length).toBe(1);

    // A server that broke is a different thing: it may well answer next time.
    const broke = scene();
    await midTurn(broke, 6);
    broke.server.refuseAt(5, settled(503), CATCH_UP_RETRY_ATTEMPTS + 3);
    broke.document.snapshot();
    await settle();
    await vi.advanceTimersByTimeAsync(CATCH_UP_RETRY_CAP_MS * CATCH_UP_RETRY_ATTEMPTS * 2);
    await settle();
    expect(broke.server.forward().filter((a) => a === 5).length).toBe(CATCH_UP_RETRY_ATTEMPTS);
  });

  it("climbs from the floor, doubling per refusal, and holds at its own cap", async () => {
    // The bottom of the jitter band, and a refusal that names no wait: what
    // is left is the ladder itself, read off the clock at each re-ask.
    const s = scene(2, () => 0);
    await midTurn(s, 6);
    const asked: number[] = [];
    const listMessages = s.server.rest.listMessages;
    s.server.rest.listMessages = (chatId, q) => {
      if (q.afterSeq === 5) asked.push(Date.now());
      return listMessages(chatId, q);
    };
    s.server.refuseAt(5, settled(503), CATCH_UP_RETRY_ATTEMPTS + 3);
    s.document.snapshot();
    await settle();
    for (let step = 0; step < 200; step += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
      await settle();
    }
    const waits = asked.slice(1).map((at, i) => at - asked[i]!);
    expect(waits).toEqual([1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000]);
  });

  it("joins a retry already armed instead of starting a second ladder", async () => {
    const s = scene();
    await midTurn(s, 6);
    s.server.refuseAt(5, refusal("4"));
    s.document.snapshot();
    await settle();
    const armed = s.server.forward().length;

    // Two more triggers arrive while the ladder is waiting — a reconnect and
    // the frame behind it, ten milliseconds apart.
    await vi.advanceTimersByTimeAsync(10);
    s.document.snapshot();
    s.document.snapshot();
    await settle();
    expect(s.server.forward().length).toBe(armed);

    // When the wait is over, ONE read goes out from the page that failed.
    await vi.advanceTimersByTimeAsync(CATCH_UP_RETRY_CAP_MS);
    await settle();
    expect(s.server.forward().filter((a) => a === 5).length).toBe(2);
  });

  it("stops claiming the chat is current once the ladder runs out, and converges", async () => {
    const unhandled = vi.fn();
    globalThis.addEventListener("unhandledrejection", unhandled);

    // The reference: the same rows, never refused, read all the way to the top.
    const clean = scene();
    await midTurn(clean, 6);
    clean.document.snapshot();
    await settle();
    await readToTop(clean.source);
    const expected = await tape(clean.source);

    const s = scene();
    await midTurn(s, 6);
    s.server.refuseAt(5, refusal(), CATCH_UP_RETRY_ATTEMPTS + 3);
    const tails = s.server.tails();
    s.events.length = 0;
    s.document.snapshot();
    await settle();
    for (let rung = 0; rung < CATCH_UP_RETRY_ATTEMPTS + 1; rung += 1) {
      await vi.advanceTimersByTimeAsync(CATCH_UP_RETRY_CAP_MS * 2);
      await settle();
    }
    // Bounded: the ladder tries and stops, rather than hammering a server that
    // is refusing or giving up on the first refusal.
    expect(s.server.forward().filter((a) => a === 5).length).toBe(CATCH_UP_RETRY_ATTEMPTS);
    // The reader is told, the way a reconnect tells them.
    expect(s.events.some((e) => e.replay === true)).toBe(true);

    // And the chat really is unread again: the next trigger opens it cold on
    // the tail, with everything above it still reachable — so a reader who
    // scrolls up sees exactly what one clean read of the same rows holds. This
    // is what the cursor and the window going TOGETHER buys: drop the cursor
    // alone and the tail's floor lands above the rows the window kept, leaving
    // a hole in the middle that no later read ever closes.
    s.document.snapshot();
    await settle();
    expect(s.server.tails()).toBe(tails + 1);
    await readToTop(s.source);
    expect(await tape(s.source)).toBe(expected);

    globalThis.removeEventListener("unhandledrejection", unhandled);
    expect(unhandled).not.toHaveBeenCalled();
  });
});
