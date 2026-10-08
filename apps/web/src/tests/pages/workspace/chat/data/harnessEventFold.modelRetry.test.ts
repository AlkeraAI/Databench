// A model call the box is retrying says so in the transcript.
//
// A node that could not reach its model gateway retried the call for an hour —
// a `retrying` row every two minutes, each carrying the harness's own sentence
// ("Cannot connect to API: Unable to connect. Is the computer able to access
// the url?") — and the transcript showed a bare "Working…" the whole time. The
// fold now keeps ONE notice for the run of retries: what kind of failure it is,
// in the reader's words, which attempt it is on, and, past a bounded number of
// attempts, what the reader should do. The box's own sentence never reaches
// the page: it is written for an operator and can name the platform's hosts.
// The notice goes the moment the model answers or the turn ends.
//
// The event shapes are the ones recorded on the gVisor node that night.

import { describe, expect, it } from "vitest";

import {
  MODEL_RETRY_LIMIT,
  createConversationFoldState,
  foldHarnessEvent,
  modelRetryClass,
  type ConversationFoldState,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-28T03:56:49Z";
const CANNOT_CONNECT =
  "Cannot connect to API: Unable to connect. Is the computer able to access the url?";

function midTurn(): ConversationFoldState {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", event_id: "u1-c", time: T, message_id: "u1", role: "user" });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "u1-t",
    time: T,
    message_id: "u1",
    part: { part_id: "u1-p", message_id: "u1", type: "text", text: "Create hello.txt" },
  });
  running(state, "s1");
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  return state;
}

function running(state: ConversationFoldState, id: string, detail: string | null = null): void {
  foldHarnessEvent(state, {
    event_type: "session.status_changed",
    event_id: id,
    time: T,
    status: "running",
    phase: "awaiting_llm",
    detail,
    turn_id: "t1",
  });
}

/** One retry as the box writes it: the status with the reason, the retry row,
 *  then the status again with none. */
function retry(state: ConversationFoldState, attempt: number, reason = CANNOT_CONNECT): void {
  running(state, `pre-${attempt}`, reason);
  foldHarnessEvent(state, {
    event_type: "retrying",
    event_id: `r-${attempt}`,
    time: T,
    reason,
    attempt,
    next_attempt_at: T,
  });
  running(state, `post-${attempt}`);
}

function notices(state: ConversationFoldState): { text: string; tone?: string }[] {
  return state.turns.flatMap((turn) =>
    turn.parts
      .filter((part) => part.kind === "system")
      .map((part) => ({ text: (part as { text: string }).text, tone: (part as { tone?: string }).tone })),
  );
}

describe("a model call the box is retrying", () => {
  it("says it cannot reach the model and is retrying, in one line", () => {
    const state = midTurn();
    retry(state, 1);
    expect(notices(state)).toEqual([{ text: "Can't reach the model. Retrying…", tone: "warning" }]);
  });

  it("stays one line across the run, counting the attempt", () => {
    const state = midTurn();
    retry(state, 1);
    retry(state, 2);
    expect(notices(state)).toEqual([{ text: "Can't reach the model. Retrying (attempt 2)…", tone: "warning" }]);
  });

  it(`past ${MODEL_RETRY_LIMIT} attempts, tells the reader what to do instead of promising`, () => {
    const state = midTurn();
    for (let attempt = 1; attempt <= 6; attempt += 1) retry(state, attempt);
    expect(notices(state)).toEqual([
      {
        text: "Can't reach the model. Still failing after 6 attempts.",
        tone: "error",
      },
    ]);
  });

  it("never shows the box's own sentence, nor any host, address or URL it names", () => {
    const state = midTurn();
    retry(state, 1, "connect ECONNREFUSED 203.0.113.7:443 (https://gateway.internal.example/v1)");
    const said = notices(state).map((n) => n.text).join(" ");
    expect(said).toBe("Can't reach the model. Retrying…");
    expect(said).not.toMatch(/\d+\.\d+\.\d+\.\d+|https?:|alkera\.ai|ECONNREFUSED|Cannot connect/);
  });

  it("goes as soon as the model answers", () => {
    const state = midTurn();
    retry(state, 1);
    foldHarnessEvent(state, {
      event_type: "part.created",
      event_id: "a1-t",
      time: T,
      message_id: "a1",
      part: { part_id: "a1-p", message_id: "a1", type: "text", text: "Created hello.txt." },
    });
    expect(notices(state)).toEqual([]);
  });

  it("goes when the turn ends", () => {
    const state = midTurn();
    retry(state, 2);
    foldHarnessEvent(state, { event_type: "session.status_changed", event_id: "end", time: T, status: "aborted", turn_id: "t1" });
    expect(notices(state).map((n) => n.text)).not.toContain("Can't reach the model. Retrying (attempt 2)…");
    expect(notices(state).some((n) => /Retrying/.test(n.text))).toBe(false);
  });
});

describe("the class a retry reason reads as", () => {
  it.each([
    [CANNOT_CONNECT, "unreachable"],
    ["getaddrinfo ENOTFOUND gateway", "unreachable"],
    ["Request timed out", "unreachable"],
    ["429 Too Many Requests", "rate_limited"],
    ["Rate limit reached for requests", "rate_limited"],
    ["Overloaded", "overloaded"],
    ["503 Service Unavailable: temporarily unavailable", "overloaded"],
    ["invalid_request_error: bad schema", "failed"],
    [null, "failed"],
  ] as const)("%s → %s", (reason, expected) => {
    expect(modelRetryClass(reason)).toBe(expected);
  });
});
