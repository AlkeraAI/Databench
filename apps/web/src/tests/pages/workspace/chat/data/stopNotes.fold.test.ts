// A Stop writes a `system` message with one synthetic text part ("Stopped by
// <person>.") into the server log. However a tab meets those rows — folded live
// off the socket, folded in reverse (part before its message), or read back in
// a page — the notice it shows is the same one: the sentence, never an empty
// block. The rows are pages of a recorded chat around three Stops,
// scrubbed the way the convergence corpus is (`[scrubbed N]` for free text, so
// the sentence is asserted by identity across views, not by its words).

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import {
  diffViews,
  describeDivergences,
  liveView,
  referenceView,
  snapshotView,
  type FoldView,
  type LogRow,
} from "@/pages/workspace/chat/data/convergence";

import stopNotes from "@/tests/fixtures/chat-stop-notes/51969efe-5321-4341-bacf-604bfad5f914/log.json";

const LOG = (stopNotes as { rows: LogRow[] }).rows;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

function eventOf(row: LogRow): HarnessEvent {
  const payload = row.payload;
  if (!isRecord(payload)) throw new Error(`row ${row.seq} has no payload`);
  if (typeof payload.event_type === "string") return payload;
  if (!isRecord(payload.payload)) throw new Error(`row ${row.seq} has no event`);
  return payload.payload;
}

/** Every `stop-<id>` note in the log: its message id and the rows that write it. */
function stopNotesIn(log: readonly LogRow[]): Array<{ messageId: string; seqs: number[] }> {
  const byMessage = new Map<string, number[]>();
  for (const row of log) {
    const match = /^(stop-[0-9a-f]+)-(created|text|done)$/.exec(row.event_id ?? "");
    if (!match) continue;
    const seqs = byMessage.get(match[1]) ?? [];
    seqs.push(row.seq);
    byMessage.set(match[1], seqs);
  }
  return [...byMessage].map(([messageId, seqs]) => ({ messageId, seqs }));
}

function noticeOf(view: FoldView, messageId: string): { text: string; parts: number } | null {
  const turn = view.turns.find((t) => t.id === messageId);
  if (!turn) return null;
  const notice = turn.parts.find((part) => part.kind === "system");
  return { text: notice && "text" in notice ? notice.text : "", parts: turn.parts.length };
}

const NOTES = stopNotesIn(LOG);

describe("a Stop note folds to the same notice however its rows arrive", () => {
  it("the fixture holds the three Stops the run recorded", () => {
    expect(NOTES.map((n) => n.messageId)).toEqual([
      "stop-05f08c882ea2",
      "stop-2f5762183ca7",
      "stop-b4db2d80750d",
    ]);
    for (const note of NOTES) expect(note.seqs).toHaveLength(3);
  });

  describe.each(NOTES)("$messageId", ({ messageId, seqs }) => {
    const [firstSeq] = seqs;
    const lastSeq = seqs[seqs.length - 1];
    // Open just before the note, in the middle of whatever the box was doing.
    const openedAt = firstSeq - 1;
    // Read to the end of the page the note sits on.
    const readTo = LOG.filter((row) => row.seq > lastSeq).find(
      (row, i, rest) => i === rest.length - 1 || rest[i + 1].seq !== row.seq + 1,
    )?.seq;
    const n = readTo ?? lastSeq;

    it("live, reversed and snapshot show one notice with the same sentence", async () => {
      const reference = await referenceView(LOG, n);
      const expected = noticeOf(reference, messageId);
      expect(expected, "the reference shows the note").not.toBeNull();
      expect(expected?.text, "the reference sentence").not.toBe("");
      expect(expected?.parts).toBe(1);

      const live = await liveView(LOG, openedAt, n);
      expect(noticeOf(live, messageId), "folded live off the socket").toEqual(expected);

      const snapshot = await snapshotView(LOG, n, { order: "socket-first" });
      expect(noticeOf(snapshot, messageId), "a fresh open, socket first").toEqual(expected);

      const restFirst = await snapshotView(LOG, n, { order: "rest-first" });
      expect(noticeOf(restFirst, messageId), "a fresh open, page first").toEqual(expected);

      const stray = [
        ...diffViews(live, reference),
        ...diffViews(snapshot, reference),
        ...diffViews(restFirst, reference),
      ].filter((d) => d.path.includes(messageId));
      expect(describeDivergences(stray)).toBe("");
    });

    it("met twice under the same ids, it is still one notice", () => {
      // The server publishes the note live the moment it writes the rows, so
      // a tab folds it off the socket and then meets the very same events
      // again in the page it reads back. They are one note, by id, and a
      // reader must not end up with the sentence on screen twice.
      const rows = LOG.filter((row) => seqs.includes(row.seq)).map(eventOf);
      const once = createConversationFoldState();
      for (const event of rows) foldHarnessEvent(once, event);
      const twice = createConversationFoldState();
      for (const event of [...rows, ...rows]) foldHarnessEvent(twice, event);
      expect(twice.turns.filter((t) => t.id === messageId)).toHaveLength(1);
      expect(twice.turns.find((t) => t.id === messageId)).toEqual(
        once.turns.find((t) => t.id === messageId),
      );
    });

    it("the part before its message still lands on the note", () => {
      const rows = LOG.filter((row) => seqs.includes(row.seq)).map(eventOf);
      const [created, text, done] = rows;
      const forward = createConversationFoldState();
      for (const event of [created, text, done]) foldHarnessEvent(forward, event);
      const reversed = createConversationFoldState();
      for (const event of [text, created, done]) foldHarnessEvent(reversed, event);
      const expected = forward.turns.find((t) => t.id === messageId);
      expect(expected?.parts).toHaveLength(1);
      expect(expected?.parts[0]).toMatchObject({ kind: "system", text: expect.any(String) });
      expect(reversed.turns.find((t) => t.id === messageId)).toEqual(expected);
    });
  });

  describe("a note whose message row is late or missing", () => {
    const rows = LOG.filter((row) => NOTES[0].seqs.includes(row.seq)).map(eventOf);
    const [created, text] = rows;
    const messageId = NOTES[0].messageId;
    const second: HarnessEvent = {
      ...text,
      part: { ...(text.part as Record<string, unknown>), part_id: `${messageId}-part-2` },
    };

    it("keeps a second synthetic part that lands before the row", () => {
      const state = createConversationFoldState();
      for (const event of [text, second, created]) foldHarnessEvent(state, event);
      const turn = state.turns.find((t) => t.id === messageId);
      expect(turn?.parts.map((part) => part.id)).toEqual([
        `${messageId}-part`,
        `${messageId}-part-2`,
      ]);
    });

    it.each([
      ["idle", { event_type: "session.status_changed", status: "idle" }],
      ["aborted", { event_type: "session.status_changed", status: "aborted", detail: null }],
    ])("shows the note when the turn ends %s without the row", (_status, terminal) => {
      const state = createConversationFoldState();
      for (const event of [text, terminal]) foldHarnessEvent(state, event);
      const turn = state.turns.find((t) => t.id === messageId);
      expect(turn?.author).toBe("system");
      expect(turn?.parts).toEqual([
        expect.objectContaining({ id: `${messageId}-part`, kind: "system", text: "[scrubbed 28]" }),
      ]);
      // Flushed once: the next end of turn has nothing to add.
      foldHarnessEvent(state, terminal);
      expect(state.turns.filter((t) => t.id === messageId)).toHaveLength(1);
      expect(state.turns.find((t) => t.id === messageId)?.parts).toHaveLength(1);
    });

    it("a steering part on an assistant message is still never shown", () => {
      const state = createConversationFoldState();
      foldHarnessEvent(state, text);
      foldHarnessEvent(state, {
        event_type: "message.created",
        message_id: messageId,
        role: "assistant",
      });
      foldHarnessEvent(state, { event_type: "session.status_changed", status: "idle" });
      expect(state.turns.find((t) => t.id === messageId)?.parts).toEqual([]);
    });
  });

  describe("the adapter's steering on the person's own message", () => {
    // What the opencode adapter puts on the wire for a turn with a per-turn
    // directive: the user message, then a synthetic <system-reminder> text
    // part (started + created in one shot, already finalized), then the words
    // the person typed. The reminder is never shown, whichever end the turn
    // comes to, and whichever of the row and the part lands first.
    const MESSAGE = "msg_user_1";
    const announced: HarnessEvent = {
      event_type: "message.created",
      message_id: MESSAGE,
      role: "user",
      time: "2026-09-21T13:52:11Z",
    };
    const reminder = { part_id: "prt_reminder", type: "text", text: "<system-reminder>\nplan rules\n</system-reminder>", synthetic: true };
    const words = { part_id: "prt_words", type: "text", text: "Write 600 words." };
    const started = (part: typeof reminder | typeof words): HarnessEvent => ({
      event_type: "part.started",
      message_id: MESSAGE,
      part_id: part.part_id,
      part_type: "text",
      initial: { id: part.part_id, messageID: MESSAGE, ...part },
    });
    const created = (part: typeof reminder | typeof words): HarnessEvent => ({
      event_type: "part.created",
      message_id: MESSAGE,
      part: { ...part, message_id: MESSAGE },
    });
    const terminals: Array<[string, HarnessEvent]> = [
      ["idle", { event_type: "session.status_changed", status: "idle" }],
      ["aborted", { event_type: "session.status_changed", status: "aborted", detail: null }],
    ];

    it.each(terminals)("is not on the person's turn when the turn ends %s", (_status, terminal) => {
      const state = createConversationFoldState();
      for (const event of [announced, started(reminder), created(reminder), started(words), created(words), terminal]) {
        foldHarnessEvent(state, event);
      }
      const turn = state.turns.find((t) => t.id === MESSAGE);
      expect(turn?.author).toBe("user");
      expect(turn?.parts.map((part) => part.kind)).toEqual(["text"]);
      expect(JSON.stringify(state.turns)).not.toContain("system-reminder");
    });

    it("is not held for the row when the part lands before it", () => {
      const state = createConversationFoldState();
      for (const event of [created(reminder), announced, created(words), { event_type: "session.status_changed", status: "idle" }]) {
        foldHarnessEvent(state, event);
      }
      const turn = state.turns.find((t) => t.id === MESSAGE);
      expect(turn?.parts.map((part) => part.kind)).toEqual(["text"]);
      expect(JSON.stringify(state.turns)).not.toContain("system-reminder");
    });
  });
});
