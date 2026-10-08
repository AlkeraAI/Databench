// The mirror stops a runaway turn by cancelling the harness and appending ONE
// durable entry that says why: `session.status_changed {status: "aborted",
// detail: "Stopped: the turn ran past its 120 s wall-clock budget (121 s)."}`
// (apps/cli/alkera_cli/cloud/mirror.py::_stop_turn). Both shells fold that entry through the
// same reader, so this pins what the reader makes of it: the reason must land
// in the transcript as a line the reader can see, and the turn must stop
// reading as "still awaiting a response" — otherwise the reader sees a turn
// that just ends, with no idea a budget was hit, and the composer's working
// state depends on the 60 s stall watchdog instead of the mirror's own word.

import { describe, expect, it } from "vitest";

import {
  conversationAwaitsResponse,
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-06T12:00:00Z";
const STOP = "Stopped: the turn ran past its 120 s wall-clock budget (121 s).";

/** A human prompt, then an assistant turn that started streaming and never
 *  finished — exactly the transcript the wall clock stops. */
function runawayTurn() {
  const state = createConversationFoldState();
  foldHarnessEvent(state, {
    event_type: "message.created",
    event_id: "u1-c",
    time: T,
    message_id: "u1",
    role: "user",
  });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "u1-t",
    time: T,
    message_id: "u1",
    part: { part_id: "u1-p", message_id: "u1", type: "text", text: "count everything" },
  });
  foldHarnessEvent(state, {
    event_type: "session.status_changed",
    event_id: "run-1",
    time: T,
    status: "running",
    turn_id: "t1",
  });
  foldHarnessEvent(state, {
    event_type: "message.created",
    event_id: "a1-c",
    time: T,
    message_id: "a1",
    role: "assistant",
  });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "a1-t",
    time: T,
    message_id: "a1",
    part: { part_id: "a1-p", message_id: "a1", type: "text", text: "Working on it" },
  });
  return state;
}

function visibleText(state: ReturnType<typeof createConversationFoldState>): string[] {
  const lines: string[] = [];
  for (const turn of state.turns) {
    for (const part of turn.parts) {
      if (part.kind === "system" || part.kind === "text") lines.push(part.text);
    }
  }
  return lines;
}

describe("the budget stop the mirror publishes", () => {
  it("is a line the reader can see", () => {
    const state = runawayTurn();
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      event_id: "budget-1",
      time: T,
      status: "aborted",
      phase: "idle",
      detail: STOP,
      turn_id: "t1",
    });
    expect(visibleText(state)).toContain(STOP);
  });

  it("ends the turn: nothing is awaiting a response any more", () => {
    const state = runawayTurn();
    expect(conversationAwaitsResponse(state.turns)).toBe(true);
    foldHarnessEvent(state, {
      event_type: "session.status_changed",
      event_id: "budget-1",
      time: T,
      status: "aborted",
      phase: "idle",
      detail: STOP,
      turn_id: "t1",
    });
    expect(conversationAwaitsResponse(state.turns)).toBe(false);
  });
});
