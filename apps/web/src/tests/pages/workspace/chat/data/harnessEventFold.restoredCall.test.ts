// A write ask answered while the workspace was asleep is acted on by the
// resumed mirror: the resume reconcile first closes the harness's interrupted
// call (`tool.call_update` error on the PART id the transcript keys it by),
// then the mirror re-runs the call and publishes the real result on the SAME
// id. The turn must read as one write that completed — not "Ran 2 tools ·
// 2 failed", which is what the reader saw when the mirror's closure landed on
// the provider's call id the ask had named, a call the transcript never had.
// The digest is `steps.length` + the error count (packages/ui Activity), so
// the tool parts of the turn are what is pinned here.

import { describe, expect, it } from "vitest";

import type { ConversationTurn, ToolConversationPart } from "@alkera/chat-model";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-15T15:00:00Z";
const PART = "prt_0a59b1d7b001VM8OTJTnKvEFLq";
const PROVIDER = "call_7d99ffc10e9946caa46758f8";
const INTERRUPTED = "interrupted — the session was restarted before this finished";
const NOT_RUN = "not run — the workspace restarted before this ran; the agent retries it";
const NOT_RUN_AS_SEEN = "Not run — the workspace restarted before this ran; the agent retries it";

function userTurn(state: ReturnType<typeof createConversationFoldState>, id: string, text: string) {
  foldHarnessEvent(state, { event_type: "message.created", event_id: `${id}-c`, time: T, message_id: id, role: "user" });
  foldHarnessEvent(state, {
    event_type: "part.created",
    event_id: `${id}-t`,
    time: T,
    message_id: id,
    part: { part_id: `${id}-p`, message_id: id, type: "text", text },
  });
}

/** The first turn as the transcript holds it once the box is back: the
 *  harness's call, the resume reconcile's interrupted closure, the ask and
 *  the reader's answer given while asleep. */
function interruptedWrite(tool: string, input: Record<string, unknown>) {
  const state = createConversationFoldState();
  userTurn(state, "u1", "create resumed.txt containing landed in this folder");
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  foldHarnessEvent(state, {
    event_type: "tool.call",
    event_id: "tc-1",
    time: T,
    tool_call_id: PART,
    provider_call_id: PROVIDER,
    message_id: "a1",
    tool_name: tool,
    input,
    status: "running",
  });
  foldHarnessEvent(state, {
    event_type: "permission.request",
    event_id: "ask-1",
    time: T,
    request_id: "per_1",
    tool_call_id: PART,
    provider_call_id: PROVIDER,
    permission_kind: "edit",
    canonical_kind: "edit",
    options: [{ option_id: "allow_once", name: "Allow once" }],
  });
  foldHarnessEvent(state, {
    event_type: "tool.call_update",
    event_id: "reconcile-1",
    time: T,
    tool_call_id: PART,
    status: "error",
    output: { error: INTERRUPTED },
    error_text: INTERRUPTED,
  });
  foldHarnessEvent(state, {
    event_type: "permission.resolved",
    event_id: "answer-per_1",
    time: T,
    request_id: "per_1",
    option_id: "allow_once",
    decided_by: "user",
  });
  return state;
}

function toolParts(turn: ConversationTurn): ToolConversationPart[] {
  return turn.parts.filter((part): part is ToolConversationPart => part.kind === "tool");
}

function assistantTurns(state: ReturnType<typeof createConversationFoldState>): ConversationTurn[] {
  return state.turns.filter((turn) => turn.author === "assistant");
}

describe("a restored ask's call after the mirror acts on the reader's answer", () => {
  it("re-run by the mirror: the ORIGINAL call reads completed, the turn has no failed step", () => {
    const state = interruptedWrite("write", { filePath: "resumed.txt", content: "landed" });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      event_id: "restored-per_1-call",
      time: T,
      tool_call_id: PART,
      status: "completed",
      input: { filePath: "resumed.txt", content: "landed" },
      output: { replayed: true, tool: "write", path: "resumed.txt", bytes: 6 },
    });
    const [turn] = assistantTurns(state);
    const tools = toolParts(turn);
    expect(tools.map((part) => [part.callId, part.state])).toEqual([[PART, "completed"]]);
    expect(tools.filter((part) => part.state === "error")).toHaveLength(0);
    expect(tools[0].output).toMatchObject({ replayed: true });
  });

  it("closed on the provider's id instead: a second, failed card appears — the shape the fix removes", () => {
    const state = interruptedWrite("write", { filePath: "resumed.txt", content: "landed" });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      event_id: "restored-per_1-call",
      time: T,
      tool_call_id: PROVIDER,
      status: "completed",
      output: { replayed: true },
    });
    const [turn] = assistantTurns(state);
    const tools = toolParts(turn);
    // The transcript's card stays interrupted and a phantom card is added: the
    // reader would see two tools, one failed. Pinned so the fold's id contract
    // (a closure lands on the call's own id, nothing else) stays visible.
    expect(tools.map((part) => part.state).sort()).toEqual(["completed", "error"]);
    expect(tools.map((part) => part.callId)).toContain(PROVIDER);
  });

  it("not re-runnable: the original closes once as not run, and the retry's own turn carries the result", () => {
    const state = interruptedWrite("bash", { command: "echo hi > viabash.txt" });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      event_id: "restored-per_1-call",
      time: T,
      tool_call_id: PART,
      status: "error",
      input: { command: "echo hi > viabash.txt" },
      output: { error: NOT_RUN },
      error_text: NOT_RUN,
    });
    // The mirror's continuation: a new user turn, and the agent's retry.
    userTurn(state, "u2", "Continue.");
    foldHarnessEvent(state, { event_type: "message.created", event_id: "a2-c", time: T, message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "tool.call",
      event_id: "tc-2",
      time: T,
      tool_call_id: "prt_retry",
      provider_call_id: "call_retry",
      message_id: "a2",
      tool_name: "bash",
      input: { command: "echo hi > viabash.txt" },
      status: "running",
    });
    foldHarnessEvent(state, {
      event_type: "tool.call_update",
      event_id: "tc-2-done",
      time: T,
      tool_call_id: "prt_retry",
      status: "completed",
      output: { stdout: "" },
    });
    const [first, second] = assistantTurns(state);
    const firstTools = toolParts(first);
    expect(firstTools.map((part) => [part.callId, part.state, part.errorText])).toEqual([
      [PART, "error", NOT_RUN_AS_SEEN],
    ]);
    expect(firstTools.filter((part) => part.state === "error")).toHaveLength(1);
    expect(toolParts(second).map((part) => [part.callId, part.state])).toEqual([
      ["prt_retry", "completed"],
    ]);
  });
});
