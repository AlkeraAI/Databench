// A tool the turn's Stop cut off ends without a result, and the fold is where
// that becomes a state the card can switch on. The harness says it one way on
// the wire: opencode's session processor, on abort, rewrites every tool still
// running to `status: "error"`, `error: "Tool execution aborted"` and
// `metadata.interrupted: true` (vendor/opencode/packages/opencode/src/session/
// processor.ts). That row must not fold as a plain failure, which would read
// "1 failed" over the harness's raw words and an empty-result notice.
//
// The neighbours stay what they are: a tool that raised is a failure, and a
// call the resume reconcile closed because the session restarted under it is a
// tool that died, which is a failure too.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Activity, resolveStep } from "@alkera/ui";

import {
  createConversationFoldState,
  foldHarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";

const T = "2026-09-23T10:00:00Z";
const ABORTED = "Tool execution aborted";
const RESTARTED = "interrupted — the session was restarted before this finished";

type Fold = ReturnType<typeof createConversationFoldState>;

function runningCall(callId: string, name = "call_tool", input: Record<string, unknown> = {}): Fold {
  const state = createConversationFoldState();
  foldHarnessEvent(state, { event_type: "message.created", event_id: "a1-c", time: T, message_id: "a1", role: "assistant" });
  foldHarnessEvent(state, {
    event_type: "tool.call",
    event_id: `${callId}-call`,
    time: T,
    tool_call_id: callId,
    message_id: "a1",
    tool_name: name,
    input,
    status: "running",
  });
  return state;
}

function update(state: Fold, callId: string, fields: Record<string, unknown>): void {
  foldHarnessEvent(state, {
    event_type: "tool.call_update",
    event_id: `${callId}-end`,
    time: T,
    tool_call_id: callId,
    ...fields,
  });
}

function toolOf(state: Fold, callId: string): ToolConversationPart {
  for (const turn of state.turns) {
    for (const part of turn.parts) {
      if (part.kind === "tool" && part.callId === callId) return part;
    }
  }
  throw new Error(`no tool part for ${callId}`);
}

describe("folding a tool the turn's Stop cut off", () => {
  it("lands the harness's abort as a stopped call, not a failed one", () => {
    const state = runningCall("prt_stop");
    update(state, "prt_stop", { status: "error", error_text: ABORTED, output: null, metadata: { interrupted: true } });
    expect(toolOf(state, "prt_stop").state).toBe("stopped");
    expect(toolOf(state, "prt_stop").streaming).toBe(false);
  });

  it("reads the flag, not the words: a scrubbed abort is still a stop", () => {
    // Logs shared outside the box carry the reason scrubbed; the flag survives.
    const state = runningCall("prt_scrubbed");
    update(state, "prt_scrubbed", { status: "error", error_text: "[scrubbed 22]", metadata: { interrupted: true } });
    expect(toolOf(state, "prt_scrubbed").state).toBe("stopped");
  });

  it("reads a row written before the flag was carried by its exact words", () => {
    const state = runningCall("prt_old");
    update(state, "prt_old", { status: "error", error_text: ABORTED });
    expect(toolOf(state, "prt_old").state).toBe("stopped");
  });

  it("does not take a failure that merely mentions an abort for a stop", () => {
    const state = runningCall("prt_sql");
    update(state, "prt_sql", { status: "error", error_text: "query aborted: statement timeout" });
    expect(toolOf(state, "prt_sql").state).toBe("error");
  });

  it("keeps a tool that raised a failure", () => {
    const state = runningCall("prt_err");
    update(state, "prt_err", { status: "error", error_text: "permission denied for relation orders" });
    expect(toolOf(state, "prt_err").state).toBe("error");
    expect(toolOf(state, "prt_err").errorText).toBe("Permission denied for relation orders");
  });

  it("keeps a call the session restart killed a failure", () => {
    const state = runningCall("prt_restart");
    update(state, "prt_restart", { status: "error", error_text: RESTARTED, output: { error: RESTARTED } });
    expect(toolOf(state, "prt_restart").state).toBe("error");
  });

  it("does not let an interrupted flag on a finished call rewrite its result", () => {
    const state = runningCall("prt_done");
    update(state, "prt_done", { status: "completed", output: { rows: 3 }, metadata: { interrupted: true } });
    expect(toolOf(state, "prt_done").state).toBe("completed");
  });

  it("lands a persisted abort part the same way a live row does", () => {
    const state = createConversationFoldState();
    foldHarnessEvent(state, { event_type: "message.created", event_id: "a2-c", time: T, message_id: "a2", role: "assistant" });
    foldHarnessEvent(state, {
      event_type: "part.created",
      event_id: "a2-p",
      time: T,
      message_id: "a2",
      part: { part_id: "prt_saved", message_id: "a2", type: "tool_call", call_id: "prt_saved", name: "bash", state: "error", error_text: ABORTED, input: { command: "sleep 40" } },
    });
    expect(toolOf(state, "prt_saved").state).toBe("stopped");
  });
});

describe("the run the folded stop renders", () => {
  it("heads the run as stopped and says it once", () => {
    const state = runningCall("prt_card");
    update(state, "prt_card", { status: "error", error_text: ABORTED, output: null, metadata: { interrupted: true } });
    const step = resolveStep(toolOf(state, "prt_card"));
    if (!step) throw new Error("no step");
    const { container } = render(
      <div className="chat-root">
        <Activity summary="Ran 1 tool call" steps={[step]} folded={false} />
      </div>,
    );
    const text = container.textContent ?? "";
    expect(container.querySelector(".chat-activity-digest")?.textContent).toContain("1 stopped");
    expect(text).not.toContain("failed");
    expect(text).not.toContain(ABORTED);
    expect(text).not.toContain("This call returned nothing.");
    expect(text.split("This tool was stopped before it finished.")).toHaveLength(2);
  });
});
