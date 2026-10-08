// A failed alkera tool reaches the transcript with its error text set to the
// CLI's envelope, as JSON: `{"error": …, "tool": …, "classification": …}`.
// The reader wants the message. The fold reads the envelope at both intakes
// — a live call update and a restored snapshot part — and leaves every other
// error text, the harness's own sentences included, exactly as it came.
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";
import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-23T10:00:00Z";
const MESSAGE = "permission denied: sharing this knowledge item was not approved";
//: The same reason as a reader sees it: the first letter raised, nothing else.
const MESSAGE_AS_SEEN = "Permission denied: sharing this knowledge item was not approved";
const ENVELOPE = JSON.stringify({ error: MESSAGE, tool: "context_note", classification: "error" });

function toolOf(state: ReturnType<typeof createConversationFoldState>): ToolConversationPart {
  const parts = cloneTurns(state).flatMap((turn) => turn.parts);
  const tool = parts.find((part): part is ToolConversationPart => part.kind === "tool");
  if (!tool) throw new Error("no tool part folded");
  return tool;
}

function liveCall(errorText: string) {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  foldHarnessEvent(state, {
    event_type: "tool.call",
    event_id: "tc-1",
    time: T,
    tool_call_id: "prt_1",
    message_id: "a1",
    tool_name: "context_note",
    input: { text: "Robin is the Tideline dev admin", visibility: "shared" },
    status: "running",
  });
  foldHarnessEvent(state, {
    event_type: "tool.call_update",
    event_id: "tc-1-done",
    time: T,
    tool_call_id: "prt_1",
    status: "error",
    error_text: errorText,
  });
  return state;
}

function restoredPart(errorText: string) {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: "a1-p",
    time: T,
    message_id: "a1",
    part: {
      part_id: "prt_1",
      message_id: "a1",
      type: "tool_call",
      call_id: "prt_1",
      name: "context_note",
      state: "error",
      input: { text: "Robin is the Tideline dev admin" },
      error_text: errorText,
    },
  });
  return state;
}

describe("a failed tool's error text", () => {
  it("is the envelope's message on a live call", () => {
    const tool = toolOf(liveCall(ENVELOPE));
    expect(tool.state).toBe("error");
    expect(tool.errorText).toBe(MESSAGE_AS_SEEN);
  });

  it("is the envelope's message on a restored part", () => {
    const tool = toolOf(restoredPart(ENVELOPE));
    expect(tool.state).toBe("error");
    expect(tool.errorText).toBe(MESSAGE_AS_SEEN);
  });

  // Anything that is not the envelope comes through whole, read as a sentence:
  // the first letter raised and nothing else, and a text that opens with an
  // identifier (here a JSON object) exactly as it came.
  it.each([
    [
      "the harness's own sentence",
      "interrupted — the session was restarted before this finished",
      "Interrupted — the session was restarted before this finished",
    ],
    ["a tool's plain text", "no such connection 'snow-prod'", "No such connection 'snow-prod'"],
    [
      "an envelope missing its classification",
      JSON.stringify({ error: MESSAGE, tool: "context_note" }),
      JSON.stringify({ error: MESSAGE, tool: "context_note" }),
    ],
  ])("is not parsed as an envelope for %s", (_what, text, seen) => {
    expect(toolOf(liveCall(text)).errorText).toBe(seen);
    expect(toolOf(restoredPart(text)).errorText).toBe(seen);
  });
});
