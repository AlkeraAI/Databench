// The stop's own sequence, read every way one log can be read.
//
// The server writes the cancellation and the "Stopped by <who>." line in one
// transaction, and the box — which already had the message — publishes its turn
// a beat later. Three claims about one message, in one order. Whichever way a
// tab reads that log (the whole of it in a page, a fresh open at the end, a tab
// that was there from the start and folded live) it must render the SAME
// message, and that message was sent.

import { describe, expect, it } from "vitest";

import stopRace from "@/tests/fixtures/chat-convergence/6ba98e93-a741-4ff4-b7ad-156d9af92e95/log.json";
import inFlight from "@/tests/fixtures/chat-convergence/3bb12bb9-4e78-415c-a480-264f134f03a0/log.json";
import {
  diffViews,
  liveView,
  referenceView,
  snapshotView,
  type LogRow,
} from "@/pages/workspace/chat/data/convergence";

const CHAT = "chat";
const AT = "2026-09-21T17:00:00.000Z";

const row = (
  seq: number,
  role: LogRow["role"],
  kind: string,
  payload: unknown,
  eventId?: string,
): LogRow =>
  ({
    id: `row-${seq}`,
    chat_id: CHAT,
    seq,
    role,
    kind,
    event_id: eventId ?? `e-${seq}`,
    payload: payload as LogRow["payload"],
    created_at: AT,
  }) as LogRow;

/** A machine event, in the envelope the server stores it in. */
const published = (seq: number, role: LogRow["role"], event: Record<string, unknown>): LogRow =>
  row(
    seq,
    role,
    String(event.event_type),
    {
      event_id: String(event.event_id),
      role,
      kind: String(event.event_type),
      payload: event,
    },
    String(event.event_id),
  );

/** The log the reported race leaves behind, in the order the server wrote it. */
const LOG: LogRow[] = [
  row(
    1,
    "user",
    "prompt",
    { kind: "prompt", text: "Deploy the staging stack", client_id: "c1", user_id: "u1" },
    "usr:c1",
  ),
  published(2, "system", {
    event_type: "prompt.cancelled",
    event_id: "cancelled-usr:c1",
    time: AT,
    session_id: CHAT,
    message_id: "usr:c1",
    client_id: "c1",
    reason: "stopped",
  }),
  published(3, "system", {
    event_type: "message.created",
    event_id: "stop-1-created",
    time: AT,
    session_id: CHAT,
    message_id: "stop-1",
    role: "system",
  }),
  published(4, "system", {
    event_type: "part.created",
    event_id: "stop-1-text",
    time: AT,
    session_id: CHAT,
    part: {
      part_id: "stop-1-part",
      message_id: "stop-1",
      type: "text",
      text: "Stopped by Admin.",
      synthetic: true,
    },
  }),
  published(5, "system", {
    event_type: "message.completed",
    event_id: "stop-1-done",
    time: AT,
    session_id: CHAT,
    message_id: "stop-1",
    finish_reason: "stop",
  }),
  published(6, "assistant", {
    event_type: "message.created",
    event_id: "a1-created",
    time: AT,
    session_id: CHAT,
    message_id: "a1",
    role: "assistant",
  }),
  published(7, "tool", {
    event_type: "tool.call",
    event_id: "a1-call",
    time: AT,
    session_id: CHAT,
    message_id: "a1",
    tool_call_id: "call-1",
    tool_name: "bash",
    status: "running",
    input: { command: "pytest" },
  }),
  published(8, "tool", {
    event_type: "tool.call_update",
    event_id: "a1-call-done",
    time: AT,
    session_id: CHAT,
    tool_call_id: "call-1",
    status: "error",
    error: "command cancelled",
  }),
  published(9, "assistant", {
    event_type: "session.status_changed",
    event_id: "stopped-1",
    time: AT,
    session_id: CHAT,
    status: "aborted",
    phase: "idle",
  }),
];

const END = LOG[LOG.length - 1].seq;

const said = (view: Awaited<ReturnType<typeof referenceView>>) =>
  view.turns.find((turn) => turn.author === "user");

describe("a stop that raced the turn it ended", () => {
  it("reads as sent, not as never sent, however the log is read", async () => {
    const reference = await referenceView(LOG, END);
    const prompt = said(reference);
    expect(prompt, "the person's message is in the view").toBeDefined();
    expect(prompt?.status).not.toBe("cancelled");
    expect(prompt?.cancelledReason).toBeUndefined();
    expect(reference.awaitsResponse, "and the turn it started is over").toBe(false);
  });

  it.each(["rest-first", "socket-first"] as const)(
    "folds the same from a fresh open (%s) as from the whole log",
    async (order) => {
      const reference = await referenceView(LOG, END);
      const fresh = await snapshotView(LOG, END, { order, windowFrom: 1 });
      expect(diffViews(fresh, reference)).toEqual([]);
    },
  );

  it("folds the same live from the start as from a fresh open", async () => {
    const fresh = await snapshotView(LOG, END, { order: "rest-first", windowFrom: 1 });
    const live = await liveView(LOG, 1, END, { order: "rest-first", windowFrom: 1 });
    expect(diffViews(live, fresh)).toEqual([]);
  });
});

describe("the log the live check left behind", () => {
  const LIVE = stopRace.rows as unknown as LogRow[];
  const LAST = LIVE[LIVE.length - 1].seq;

  it("is the log that was pulled", async () => {
    // The shape the claims below are about: four prompts, each cancelled, and
    // one of them (seq 1) answered anyway — the paragraph at 35 is the row
    // the box now never publishes, kept here because this is what it DID.
    expect(LIVE.filter((r) => r.kind === "prompt").map((r) => r.seq)).toEqual([1, 43, 48, 67]);
    expect(LIVE.filter((r) => r.kind === "prompt.cancelled").map((r) => r.seq)).toEqual([
      2, 44, 49, 68,
    ]);
    const paragraph = LIVE.find((r) => r.seq === 35);
    expect(paragraph?.kind).toBe("part.created");
    expect(paragraph?.role).toBe("assistant");
  });

  it("reads each message by whether its turn ever ran", async () => {
    const view = await referenceView(LIVE, LAST);
    const prompts = view.turns.filter((turn) => turn.author === "user" && turn.parts.length > 0);
    const byWords = (turn: (typeof prompts)[number]) =>
      turn.parts.some((part) => part.kind === "text");
    expect(prompts.filter(byWords).length).toBeGreaterThanOrEqual(4);
    // The first message: the box went running and wrote a paragraph, so it was
    // sent — whatever the server recorded in the moment before its stop landed.
    const first = prompts[0];
    expect(first.status).not.toBe("cancelled");
    expect(first.cancelledReason).toBeUndefined();
    // The one whose turn never started keeps its line.
    const neverRan = prompts.find((turn) => turn.status === "cancelled");
    expect(neverRan?.cancelledReason).toBe("stopped");
  });
});

describe("a stop that landed while the cancel was still in flight", () => {
  const LOG = inFlight.rows as unknown as LogRow[];

  /** The attempt the live check scored: the prompt at 113, its cancellation and
   *  the stop's note, and then — because the drop was armed only after the
   *  cancel came back — everything the turn went on publishing across it. */
  const ATTEMPT = LOG.filter((row) => row.seq >= 113 && row.seq <= 129);

  it("is the log that was pulled", () => {
    expect(ATTEMPT[0].kind).toBe("prompt");
    expect(ATTEMPT[1].kind).toBe("prompt.cancelled");
    // Published between the stop and its terminal, which is the bug: the box's
    // own `running`, its echo of the message, and the shell of the answer.
    expect(ATTEMPT.filter((r) => r.kind === "session.status_changed").length).toBe(4);
    expect(
      ATTEMPT.some((r) => r.role === "assistant" && r.kind === "message.created"),
    ).toBe(true);
  });

  it("reads as sent once nothing of the turn is published but its end", async () => {
    // The same attempt as the box records it NOW: the cancellation, the note,
    // and the one row that says a turn existed. Nothing in between, because
    // nothing in between is this chat's record any more.
    const quiet = ATTEMPT.filter(
      (row) =>
        row.seq <= 117 || String(row.event_id).startsWith("stopped-"),
    );
    const view = await referenceView(quiet, 129);
    const prompt = view.turns.find((turn) => turn.author === "user");
    expect(prompt?.status).not.toBe("cancelled");
    expect(prompt?.cancelledReason).toBeUndefined();
    expect(view.awaitsResponse).toBe(false);
  });
});
