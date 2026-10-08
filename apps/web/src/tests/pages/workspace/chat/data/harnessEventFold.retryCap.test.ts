// A turn the harness stopped because its model call kept failing.
//
// The harness stops the agent first, so the agent's own "aborted" for the turn
// lands just before the failure that says why: retrying ×N, then aborted, then
// an error whose detail names the kind and the count. The reader must see that
// sentence as the turn's ending — never a bare stop, and never a generic
// rewording that drops the count.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type ConversationFoldState,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-28T06:10:00Z";
const CANNOT_CONNECT = "Cannot connect to API: Unable to connect. Is the computer able to access the url?";

function midTurn(): ConversationFoldState {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", event_id: "u1-c", time: T, message_id: "u1", role: "user" });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "u1-t",
    time: T,
    message_id: "u1",
    part: { part_id: "u1-p", message_id: "u1", type: "text", text: "Count the mentions" },
  });
  status(state, "s1", "running");
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  return state;
}

function status(state: ConversationFoldState, id: string, value: string, extra: Record<string, unknown> = {}): void {
  foldHarnessEvent(state, {
    event_type: "session.status_changed",
    event_id: id,
    time: T,
    status: value,
    turn_id: "t1",
    ...(value === "running" ? { phase: "awaiting_llm" } : {}),
    ...extra,
  });
}

/** The recorded order: every retry, the agent's own close, then the failure. */
function stoppedForRetrying(reason: string, failure: string, attempts = 6): ConversationFoldState {
  const state = midTurn();
  for (let attempt = 1; attempt <= attempts; attempt++) {
    status(state, `pre-${attempt}`, "running", { detail: reason });
    foldHarnessEvent(state, { event_type: "retrying", event_id: `r-${attempt}`, time: T, reason, attempt, next_attempt_at: T });
    status(state, `post-${attempt}`, "running");
  }
  status(state, "agent-close", "aborted");
  status(state, "failed", "error", { phase: "error", detail: failure });
  return state;
}

function visibleText(state: ConversationFoldState): string[] {
  return state.turns.flatMap((turn) =>
    turn.parts.flatMap((part) => ("text" in part && typeof part.text === "string" ? [part.text] : [])),
  );
}

describe("a turn stopped by the model-retry cap", () => {
  it.each([
    ["unreachable", CANNOT_CONNECT, "The model could not be reached after 6 attempts; the turn was stopped."],
    ["overloaded", "Overloaded", "The provider was overloaded after 6 attempts; the turn was stopped."],
    ["rate limited", "429 Too Many Requests", "The provider refused the request after 6 attempts; the turn was stopped."],
    ["unavailable", "503 Service Unavailable", "The provider was unavailable after 6 attempts; the turn was stopped."],
    ["out of time", CANNOT_CONNECT, "The model could not be reached after 4 attempts in 5 minutes; the turn was stopped."],
  ])("ends %s with the harness's own sentence, as a failure", (_label, reason, failure) => {
    const state = stoppedForRetrying(reason, failure);
    const errors = state.turns.filter((turn) => turn.status === "error");
    expect(errors).toHaveLength(1);
    const [part] = errors[0].parts;
    expect(part).toMatchObject({ kind: "system", tone: "error", text: failure });
    // The failure is the last word on the turn: nothing after it reads as the ending.
    expect(state.turns[state.turns.length - 1]).toBe(errors[0]);
  });

  it("shows no bare stop and no retry line once the failure lands", () => {
    const failure = "The model could not be reached after 6 attempts; the turn was stopped.";
    const state = stoppedForRetrying(CANNOT_CONNECT, failure);
    const text = visibleText(state);
    expect(text.filter((line) => /aborted/i.test(line))).toEqual([]);
    expect(text.filter((line) => /retrying|attempt \d/i.test(line) && line !== failure)).toEqual([]);
    expect(state.sessionWorking).toBe(false);
  });

  it("keeps the turn the model was working on settled, not streaming", () => {
    const state = stoppedForRetrying(CANNOT_CONNECT, "The model could not be reached after 6 attempts; the turn was stopped.");
    for (const turn of state.turns) {
      for (const part of turn.parts) expect(part.streaming ?? false).toBe(false);
    }
  });
});
