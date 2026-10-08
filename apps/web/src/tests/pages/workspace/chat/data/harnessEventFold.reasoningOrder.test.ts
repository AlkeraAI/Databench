// Where a thought goes, and when it is finished.
//
// A reasoning part opens carrying no text at all: `part.started` names it, the
// words arrive as token frames, and a finalized `part.created` closes it. In
// the browser those do not arrive on one lane. The portal reads a workspace
// machine's mirror, which puts every durable event on the doc the moment it
// happens and coalesces token frames onto a separate, timed lane — so the
// thought's words routinely land AFTER the tool call the model ran next, and
// its closing frame can still be in flight while the reader watches.
//
// Folded naively that produces thoughts
// stacked under the tool calls that came after them, each still labelled
// "Thinking…" with its whole paragraph held open.

import { describe, expect, it } from "vitest";

import {
  createConversationFoldState,
  foldHarnessEvent,
  type ConversationFoldState,
} from "@/pages/workspace/chat/data/harnessEventFold";

const MSG = "msg_07a95d9440019XTb0fd5bVIlpB";
const THOUGHT = "prt_07a95fe8a001XYNsXUPi6ylE9e";
const THOUGHT_TEXT = "The table is in the staging schema, so I should read the file first.";

function openAssistant(): ConversationFoldState {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", message_id: MSG, role: "assistant" });
  return state;
}

/** As recorded: a reasoning `part.started` carries an empty text. */
function thoughtOpens(state: ConversationFoldState, partId = THOUGHT): void {
  foldHarnessEvent(state, {
    event_type: "part.started",
    part_id: partId,
    part_type: "reasoning",
    message_id: MSG,
    initial: { id: partId, text: "", type: "reasoning", messageID: MSG },
  });
}

function thoughtTokens(state: ConversationFoldState, text: string, partId = THOUGHT): void {
  foldHarnessEvent(state, {
    event_type: "agent.thought_chunk",
    message_id: MSG,
    part_id: partId,
    text,
  });
}

function thoughtCloses(state: ConversationFoldState, text: string, partId = THOUGHT): void {
  foldHarnessEvent(state, {
    event_type: "part.created",
    part: { part_id: partId, message_id: MSG, type: "reasoning", text },
  });
}

function toolRuns(state: ConversationFoldState, callId: string, name = "read"): void {
  foldHarnessEvent(state, {
    event_type: "tool.call",
    message_id: MSG,
    tool_call_id: callId,
    tool_name: name,
    status: "running",
    input: { path: "staging.sql" },
  });
}

/** What the transcript actually reads as, top to bottom. */
function shape(state: ConversationFoldState): { kind: string; id: string; streaming: boolean }[] {
  return state.turns
    .flatMap((turn) => turn.parts)
    .map((part) => ({ kind: part.kind, id: part.id, streaming: part.streaming === true }));
}

describe("a thought whose words arrive after the work that followed it", () => {
  it("stays above the tool the model ran next, and reads as finished", () => {
    const state = openAssistant();
    thoughtOpens(state);
    toolRuns(state, "call_read");
    // The mirror's coalescing timer catches up only now.
    thoughtTokens(state, THOUGHT_TEXT);
    expect(shape(state)).toEqual([
      { kind: "thinking", id: THOUGHT, streaming: false },
      { kind: "tool", id: "call_read", streaming: true },
    ]);
  });

  it("is one block, not a second one, when its closing frame finally lands", () => {
    const state = openAssistant();
    thoughtOpens(state);
    toolRuns(state, "call_read");
    thoughtTokens(state, THOUGHT_TEXT);
    thoughtCloses(state, THOUGHT_TEXT);
    const thinking = state.turns
      .flatMap((turn) => turn.parts)
      .filter((part) => part.kind === "thinking");
    expect(thinking).toHaveLength(1);
    expect("text" in thinking[0] ? thinking[0].text : "").toBe(THOUGHT_TEXT);
    expect(shape(state)[0]).toEqual({ kind: "thinking", id: THOUGHT, streaming: false });
  });

  it("does not re-open when a coalesced frame lands after the tool call", () => {
    const state = openAssistant();
    thoughtOpens(state);
    thoughtTokens(state, "The table is in the staging schema, ");
    toolRuns(state, "call_read");
    // The tail of the same thought, flushed a beat late.
    thoughtTokens(state, "so I should read the file first.");
    const [first] = shape(state);
    expect(first).toEqual({ kind: "thinking", id: THOUGHT, streaming: false });
    const thinking = state.turns.flatMap((t) => t.parts).find((p) => p.kind === "thinking");
    expect(thinking && "text" in thinking ? thinking.text : "").toBe(THOUGHT_TEXT);
  });

  it("keeps several thoughts in the order they were thought", () => {
    const state = openAssistant();
    thoughtOpens(state, "prt_one");
    thoughtOpens(state, "prt_two");
    toolRuns(state, "call_read");
    // Both flushed after the call, second one first.
    thoughtTokens(state, "Second.", "prt_two");
    thoughtTokens(state, "First.", "prt_one");
    expect(shape(state)).toEqual([
      { kind: "thinking", id: "prt_one", streaming: false },
      { kind: "thinking", id: "prt_two", streaming: false },
      { kind: "tool", id: "call_read", streaming: true },
    ]);
  });

  it("still reads as in progress while it is the only thing happening", () => {
    const state = openAssistant();
    thoughtOpens(state);
    thoughtTokens(state, "The table is in the staging schema, ");
    expect(shape(state)).toEqual([{ kind: "thinking", id: THOUGHT, streaming: true }]);
  });

  it("is finished by the answer that follows it, not left thinking under it", () => {
    const state = openAssistant();
    thoughtOpens(state);
    thoughtTokens(state, THOUGHT_TEXT);
    foldHarnessEvent(state, {
      event_type: "part.started",
      part_id: "prt_answer",
      part_type: "text",
      message_id: MSG,
      initial: { id: "prt_answer", text: "", type: "text", messageID: MSG },
    });
    foldHarnessEvent(state, {
      event_type: "agent.message_chunk",
      message_id: MSG,
      part_id: "prt_answer",
      text: "The staging table has 40 rows.",
    });
    expect(shape(state)).toEqual([
      { kind: "thinking", id: THOUGHT, streaming: false },
      { kind: "text", id: "prt_answer", streaming: true },
    ]);
  });
});
