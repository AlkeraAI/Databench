// `pageBefore` is the reference model of the server's page rule that the
// convergence corpus and the browser detector fold against. The server lowers
// a backward page until its first row both starts a turn and opens every
// message the page carries: the reach for the turn's prompt row and the reach
// past any message the boundary would split are alternated to a joint fixed
// point, a message is held whole only when the page holds the row that OPENED
// it, and `cut` is said only when the descent stopped on its budget with a
// message still unaccounted. The cases are the server's own
// (`apps/backend/tests/test_chat_transcript_paging.py`), so the model is pinned
// to the rule and not to a reading of it; the rule from before it stays
// selectable, because a portal may still be talking to that server.

import { describe, expect, it } from "vitest";

import {
  alignBoundary,
  messageIdOf,
  pageBefore,
  pageBudget,
  unaccountedMessages,
  type Boundary,
  type LogRow,
  type PageReach,
  type PageRow,
} from "@/pages/workspace/chat/data/convergence";

import geo from "@/tests/fixtures/chat-convergence/3f0f77f4-b296-4c47-bdf3-3dd55ce36256/log.json";
import rerun from "@/tests/fixtures/chat-convergence/11621878-7b8a-4ba6-8478-ee54378fa7af/log.json";
import granite from "@/tests/fixtures/chat-convergence/5e1c988b-de50-468d-bb77-9a92f5f5c00f/log.json";

const rowsOf = (fixture: unknown) => (fixture as { rows: LogRow[] }).rows;
const GRANITE = rowsOf(granite);

// --- the server tests' builders ------------------------------------------------------

interface Spec {
  role: string;
  kind: string;
  message: string | null;
  onPart: boolean;
}
const prompt = (): Spec => ({ role: "user", kind: "prompt", message: null, onPart: false });
const event = (kind: string, message: string | null = null, opts: { role?: string; onPart?: boolean } = {}): Spec => ({
  role: opts.role ?? "assistant",
  kind,
  message,
  onPart: opts.onPart ?? false,
});
/** A whole message's rows: the open, `parts` part rows, the close. */
const message = (name: string, parts: number, role = "assistant"): Spec[] => [
  event("message.created", name, { role }),
  ...Array.from({ length: parts }, (_, index) =>
    event(index % 2 === 0 ? "part.started" : "part.created", name, { role, onPart: index % 2 === 1 }),
  ),
  event("message.completed", name, { role }),
];

/** Consecutive rows from sequence 1, in the envelope the machine's events are stored in. */
function script(specs: Spec[]): LogRow[] {
  return specs.map((spec, index) => {
    const seq = index + 1;
    const inner: Record<string, unknown> = { event_type: spec.kind };
    if (spec.message !== null) {
      if (spec.onPart) inner.part = { part_id: `p${seq}`, message_id: spec.message, type: "text", text: "…" };
      else inner.message_id = spec.message;
    }
    const payload = spec.kind === "prompt" ? { kind: "prompt", text: `said ${seq}` } : { kind: spec.kind, role: spec.role, payload: inner };
    return { id: `r${seq}`, chat_id: "c", seq, role: spec.role, kind: spec.kind, event_id: `e${seq}`, payload, created_at: "t" } as unknown as LogRow;
  });
}

const seqs = (rows: readonly LogRow[]) => rows.map((row) => row.seq);
const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
const LIMIT = 100;

describe("which message a row belongs to, for paging", () => {
  it("is the id the row states — flat, or on the part — whoever published it", () => {
    const rows = script([
      event("message.created", "answer"),
      event("part.created", "answer", { onPart: true }),
      event("message.created", "echo", { role: "user" }),
      event("message.created", "stop-1", { role: "system" }),
      prompt(),
      event("session.status_changed", null, { role: "system" }),
    ]);
    expect(rows.map(messageIdOf)).toEqual(["answer", "answer", "echo", "stop-1", null, null]);
  });

  it("except a cancelled prompt's row, whose id names the prompt it cancels", () => {
    const [cancelled] = script([event("prompt.cancelled", "usr:web-1", { role: "system" })]);
    expect(messageIdOf(cancelled)).toBeNull();
  });

  it("a message is accounted for only by the row that opened it", () => {
    const rows = script([
      event("part.started", "tail-only"),
      event("message.created", "whole"),
      event("part.started", "whole"),
    ]);
    expect([...unaccountedMessages(rows)]).toEqual(["tail-only"]);
  });
});

describe("the page the server hands back", () => {
  it("lowers the boundary by each reach in turn until both hold (the server's five-step case)", () => {
    const rows = script([
      prompt(),
      ...message("pre", 8), // 2..11
      prompt(), // 12 — the turn `w` answers
      event("message.created", "w"), // 13
      ...Array.from({ length: 6 }, () => event("part.started", "w")), // 14..19
      prompt(), // 20 — sent while `w` was still writing
      ...Array.from({ length: 4 }, () => event("part.started", "w")), // 21..24
      event("message.completed", "w"), // 25
      event("message.created", "outer"), // 26
      event("part.started", "outer"), // 27
      prompt(), // 28 — inside `outer`
      event("message.created", "inner"), // 29
      event("part.started", "inner"), // 30
      prompt(), // 31 — inside `inner` AND `outer`
      event("part.created", "inner", { onPart: true }), // 32
      event("message.completed", "inner"), // 33
      event("part.started", "outer"), // 34
      event("message.completed", "outer"), // 35
      ...message("last", 98), // 36..135
    ]);
    const tail = pageBefore(rows, null, LIMIT);
    expect([tail.items[0].seq, tail.items[0].kind]).toEqual([12, "prompt"]);
    expect([tail.prev_before, tail.has_older, tail.cut]).toEqual([12, true, false]);
    expect(seqs(tail.items)).toEqual(range(12, 135));
  });

  it("a prompt recorded inside an answer does not cut that answer in half", () => {
    const rows = script([
      prompt(), // 1
      event("message.created", "answer"), // 2
      ...Array.from({ length: 5 }, () => event("part.started", "answer")), // 3..7
      prompt(), // 8 — inside `answer`
      event("part.created", "answer", { onPart: true }), // 9
      event("message.completed", "answer"), // 10
      ...message("next", 4), // 11..16
    ]);
    const tail = pageBefore(rows, null, 6);
    expect([tail.items[0].seq, tail.items[0].kind]).toEqual([1, "prompt"]);
    expect(tail.cut).toBe(false);
    // The server before the rule opened it on the prompt inside the answer.
    const before = pageBefore(rows, null, 6, undefined, null);
    expect(before.items[0].seq).toBe(8);
    expect([...unaccountedMessages(before.items)]).toEqual(["answer"]);
  });

  it("a message longer than the reach is flagged, and the page below completes it", () => {
    const limit = 4;
    // The tail lands inside the long message, so the page CARRIES it.
    const rows = script([prompt(), ...message("long", 60)]);
    const tail = pageBefore(rows, null, limit);
    expect(tail.cut).toBe(true);
    expect([...unaccountedMessages(tail.items)]).toContain("long");
    // The descent's ceiling: the limit, plus limit × (turn reach + message reach).
    expect(tail.items.length).toBe(limit + limit * (4 + 2));
    // Walking down from it reaches the message's opening with no gap and no repeat.
    const walked = [...tail.items];
    let before = tail.prev_before ?? null;
    for (let guard = 0; guard < 40 && before !== null && before > 1; guard += 1) {
      const page = pageBefore(rows, before, limit);
      expect(page.items[page.items.length - 1].seq).toBe(walked[0].seq - 1);
      walked.unshift(...page.items);
      before = page.prev_before ?? null;
    }
    expect(seqs(walked)).toEqual(range(1, rows.length));
  });

  it("says nothing is cut when the read got all the way down to the first row", () => {
    // A message with no opening row at all, short enough to read to row 1:
    // there is nothing below, so the page is whole and does not say cut.
    const rows = script([
      event("part.started", "never-opened"),
      event("part.created", "never-opened", { onPart: true }),
      event("part.started", "never-opened"),
    ]);
    const tail = pageBefore(rows, null, 2);
    expect([...unaccountedMessages(tail.items)]).toEqual(["never-opened"]);
    expect(tail.items[0].seq).toBe(1);
    expect(tail.cut).toBe(false);
    expect(tail.has_older).toBe(false);
  });

  it("a cancelled prompt row does not make every page say it is cut", () => {
    const rows = script([
      prompt(),
      ...message("a", 3),
      prompt(),
      event("prompt.cancelled", "usr:web-9", { role: "system" }),
      prompt(),
      ...message("b", 3),
    ]);
    for (const limit of [3, 5, 8]) {
      let before: number | null = null;
      for (let guard = 0; guard < 20; guard += 1) {
        const page = pageBefore(rows, before, limit);
        if (page.items.length === 0) break;
        expect(page.cut, `limit ${limit} before ${before}`).toBe(false);
        if (!page.has_older) break;
        before = page.prev_before ?? null;
      }
    }
  });

  it.each([
    ["3f0f77f4", rowsOf(geo)],
    ["11621878", rowsOf(rerun)],
    ["5e1c988b", GRANITE],
  ])("the recorded chat %s opens its tail on a person's message, holding every message it carries", (_name, log) => {
    const tail = pageBefore(log, null, 200);
    expect(tail.items[0].kind).toBe("prompt");
    expect(tail.cut).toBe(false);
    expect([...unaccountedMessages(tail.items)]).toEqual([]);
  });

  it("walks a recorded chat as a partition: every row once, no page opening inside a message", () => {
    for (const limit of [25, 60, 200]) {
      const walked: LogRow[] = [];
      let before: number | null = null;
      for (let guard = 0; guard < 400; guard += 1) {
        const page = pageBefore(GRANITE, before, limit);
        if (page.items.length === 0) break;
        if (!page.cut) expect([...unaccountedMessages(page.items)], `limit ${limit} before ${before}`).toEqual([]);
        walked.unshift(...page.items);
        if (!page.has_older) break;
        before = page.prev_before ?? null;
      }
      expect(seqs(walked)).toEqual(seqs(GRANITE));
    }
  });

  it("the granite chat's tail, before the rule, opened inside the stopped message", () => {
    const page = pageBefore(GRANITE, null, 200, undefined, null);
    expect(page.items[0].seq).toBe(170);
    expect(page.cut).toBe(false);
    expect([...unaccountedMessages(page.items)]).toContain("msg_0c521b5d8001n8EMpjkyvlUGS3");
  });
});

// --- the rule's own trap shapes ------------------------------------------------------
//
// `packages/api-core/tests/objects/test_transcript_page_rule.py`, the
// hand-built shapes, against the mirror of `align_boundary` — driven the way
// that file drives it: handed exactly the budget under the page, or the whole
// log, and expected to answer the same either way.

describe("alignBoundary, on the shapes that trap it", () => {
  const LIMIT = 20;
  const REACH: PageReach = { limit: LIMIT, turn: 4, message: 2 };
  const BUDGET = pageBudget(REACH);

  type Shape = [startsTurn: boolean, messageId: string | null, opensMessage: boolean];
  const aPrompt = (): Shape => [true, null, false];
  const aPart = (name: string): Shape => [false, name, false];
  const opening = (name: string): Shape => [false, name, true];
  const plain = (): Shape => [false, null, false];
  const times = <T,>(n: number, make: () => T): T[] => Array.from({ length: n }, make);
  const rowsOf = (shapes: Shape[]): PageRow[] =>
    shapes.map(([startsTurn, messageId, opensMessage], index) => ({
      seq: index + 1,
      startsTurn,
      messageId,
      opensMessage,
    }));
  const spansOf = (rows: PageRow[]): Map<string, [number, number]> => {
    const spans = new Map<string, [number, number]>();
    for (const row of rows) {
      if (row.messageId === null) continue;
      const [low, high] = spans.get(row.messageId) ?? [row.seq, row.seq];
      spans.set(row.messageId, [Math.min(low, row.seq), Math.max(high, row.seq)]);
    }
    return spans;
  };
  const splitBy = (spans: Map<string, [number, number]>, boundary: number): string[] =>
    [...spans].filter(([, [low, high]]) => low < boundary && boundary <= high).map(([name]) => name).sort();

  /** The rule handed the whole log, and handed only the budget under the page:
   *  the two must agree, and the agreed answer is what is returned. */
  function settle(rows: PageRow[], pageLow: number): Boundary {
    const floor = Math.max(pageLow - BUDGET, 1);
    const whole = alignBoundary(rows, { pageLow, reach: REACH });
    const budgeted = alignBoundary(rows.filter((row) => row.seq >= floor), { pageLow, reach: REACH });
    expect(budgeted).toEqual(whole);
    return whole;
  }

  it("has the budget the server has", () => {
    expect(BUDGET).toBe(LIMIT * (4 + 2));
  });

  it("a page opens on the turn its first row belongs to", () => {
    // The reader's next prompt landed inside the answer before it, so the row
    // that starts the turn is under the answer's own opening row.
    const rows = rowsOf([
      aPrompt(),
      opening("m0"),
      aPart("m0"),
      aPrompt(),
      opening("m1"),
      aPart("m1"),
      aPrompt(),
      aPart("m1"),
      aPart("m1"),
      opening("m2"),
      ...times(LIMIT + 2, () => aPart("m2")),
    ]);
    expect(settle(rows, rows.length - LIMIT + 1)).toEqual({ seq: 4, cut: false });
    expect(rows[3].startsTurn).toBe(true);
  });

  it("a message the page cannot open is reported, not carried", () => {
    // Its rows sit further apart than the read goes: the page holds its end,
    // cannot reach its start, and says so.
    const rows = rowsOf([
      aPrompt(),
      opening("far"),
      aPart("far"),
      ...times(BUDGET + LIMIT, plain),
      aPrompt(),
      aPart("near"),
      aPart("near"),
      opening("near"),
      aPart("far"),
      ...times(LIMIT - 1, () => aPart("near")),
    ]);
    const pageLow = rows.length - LIMIT + 1;
    const settled = settle(rows, pageLow);
    expect(settled.cut).toBe(true);
    expect(settled.seq).toBeGreaterThanOrEqual(pageLow - BUDGET);
  });

  it("a re-announced opening row is not proof when the descent spent its budget arriving", () => {
    // `message.created` is re-announced from inside a turn, so holding one
    // proves nothing when no row under the page may be looked at.
    const rows = rowsOf([
      opening("echo"),
      aPart("echo"),
      aPrompt(),
      opening("answer"),
      ...times(BUDGET - 3, () => aPart("answer")),
      opening("echo"),
      ...times(LIMIT, () => aPart("answer")),
    ]);
    const pageLow = rows.length - LIMIT + 1;
    expect(pageLow - BUDGET, "the descent's floor is the prompt row").toBe(3);
    expect(settle(rows, pageLow)).toEqual({ seq: 3, cut: true });
  });

  it("a page that holds nothing of any message settles on the turn alone, and says when it has none", () => {
    // Rows belonging to no message cannot be anyone's end, so nothing but the
    // turn start can move the boundary.
    const rows = rowsOf([aPrompt(), ...times(LIMIT * 3, plain)]);
    expect(settle(rows, LIMIT * 2 + 1)).toEqual({ seq: 1, cut: false });
    // No turn start within reach: the page admits it opens mid-turn.
    const noTurn = rowsOf(times(LIMIT * 3, plain));
    expect(settle(noTurn, LIMIT * 2 + 1).cut).toBe(true);
  });

  it("an opening row inside the page is checked against the whole budget", () => {
    // The message's other row sits well under the page with nothing of the
    // message in between, and the opening row the page holds is the box's
    // SECOND announcement: the proof window is the whole budget.
    const rows = rowsOf([
      aPrompt(),
      aPart("m"),
      plain(),
      ...times(LIMIT + 4, plain),
      aPrompt(),
      opening("m"),
      opening("n"),
      ...times(LIMIT - 2, () => aPart("n")),
    ]);
    const spans = spansOf(rows);
    const pageLow = rows.length - LIMIT + 1;
    const [mLow] = spans.get("m") as [number, number];
    expect(mLow).toBeLessThan(pageLow - LIMIT);
    expect(mLow).toBeGreaterThan(Math.max(pageLow - BUDGET, 1));
    const settled = settle(rows, pageLow);
    expect(settled.seq).toBeLessThanOrEqual(mLow);
    expect(splitBy(spans, settled.seq)).toEqual([]);
  });

  it("a page that cannot look a message under itself says so", () => {
    // A descent that ends a row or two above its floor has read almost nothing
    // under the page, and an opening row inside the page proves nothing
    // there: the box re-announces one from inside the turn. The rows can be
    // three apart; it is the boundary's distance from the FLOOR that decides
    // whether anything could have been seen.
    const settlesAt = LIMIT * 3 + 1;
    const head: Shape[] = [aPart("m"), ...times(settlesAt - 1, plain), aPrompt(), opening("m"), opening("a")];
    const rows = rowsOf([...head, ...times(settlesAt + BUDGET + LIMIT - 1 - head.length, () => aPart("a"))]);
    const spans = spansOf(rows);
    const pageLow = rows.length - LIMIT + 1;
    const floor = Math.max(pageLow - BUDGET, 1);
    expect(floor).toBe(settlesAt);
    expect(spans.get("m")).toEqual([1, settlesAt + 2]);
    const settled = settle(rows, pageLow);
    expect(settled.seq - floor).toBeLessThan(LIMIT * REACH.message);
    expect(splitBy(spans, settled.seq)).toEqual(["m"]);
    expect(settled.cut).toBe(true);
  });

  it("a message with a gap wider than its reach is the rule's known exposure", () => {
    // Pinned as the rule pins it: `m` has a row under the page's floor, its
    // re-announced opening row inside the page, and not one row of it in the
    // whole stretch between — a gap wider than a message's reach. The page
    // holds its end and reports itself whole. Mirrored as is, so the two
    // sides cannot disagree on it.
    const rows = rowsOf([
      aPart("m"),
      aPrompt(),
      ...times(BUDGET + LIMIT, plain),
      aPrompt(),
      opening("m"),
      opening("n"),
      ...times(LIMIT - 3, () => aPart("n")),
    ]);
    const spans = spansOf(rows);
    const pageLow = rows.length - LIMIT + 1;
    const floor = Math.max(pageLow - BUDGET, 1);
    expect((spans.get("m") as [number, number])[0]).toBeLessThan(floor);
    const settled = settle(rows, pageLow);
    expect(settled.seq - floor).toBeGreaterThanOrEqual(LIMIT * REACH.message);
    expect(splitBy(spans, settled.seq)).toEqual(["m"]);
    expect(settled.cut).toBe(false);
  });

  it("a transcript whose first row is not 1 is not reported as cut for opening on it", () => {
    const rows = rowsOf([...times(3, plain), aPrompt(), opening("m"), ...times(LIMIT, () => aPart("m"))]).filter(
      (row) => row.seq >= 4,
    );
    expect(alignBoundary(rows, { pageLow: rows.length - LIMIT + 4, oldest: 4, reach: REACH })).toEqual({
      seq: 4,
      cut: false,
    });
  });
});
