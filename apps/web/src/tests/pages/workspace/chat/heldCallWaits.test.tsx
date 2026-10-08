// While an ask waits on a person, the command it gates has not run. The tape
// read "Running 1 terminal command" over a loading sweep before anyone had
// approved; it now reads "Waiting to run …" until the ask is answered, and the
// same call reads as running once the ask is resolved.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Activity } from "@alkera/ui";
import {
  cloneTurns,
  createConversationFoldState,
  foldHarnessEvent,
  type HarnessEvent,
} from "@/pages/workspace/chat/data/harnessEventFold";
import { heldTurnIds, transcriptEntries } from "@/pages/workspace/chat/entries";
import { stripPermissionParts } from "@/pages/workspace/chat/stripPermissionParts";

const NOOP = () => {};
const CTX = {
  onOpenUrl: NOOP,
  onLinkClick: NOOP,
  onResourceOpen: NOOP,
  onSubagentOpen: NOOP,
  onPlanOpen: NOOP,
  onLineageOpen: NOOP,
  onKnowledgeOpen: NOOP,
  questionLive: null,
};

const CALL: HarnessEvent = {
  event_type: "tool.call",
  message_id: "msg-1",
  tool_call_id: "call-rm",
  tool_name: "bash",
  status: "pending",
  input: { command: "rm -rf build" },
};

const ASK = (prompting: boolean): HarnessEvent => ({
  event_type: "permission.request",
  message_id: "msg-1",
  request_id: "per_1",
  permission_kind: "bash",
  patterns: ["rm -rf build"],
  prompting,
  options: [
    { option_id: "allow_once", name: "Allow once" },
    { option_id: "reject_once", name: "Reject once" },
  ],
});

function fold(events: HarnessEvent[]) {
  const state = createConversationFoldState();
  for (const event of [{ event_type: "message.created", message_id: "msg-1", role: "assistant" }, ...events]) {
    foldHarnessEvent(state, event);
  }
  return cloneTurns(state);
}

/** The tape as ChatSurface builds it: open asks stripped, held turns read first. */
function tape(events: HarnessEvent[]): string {
  const turns = fold(events);
  const entries = transcriptEntries(stripPermissionParts(turns), { ...CTX, heldTurnIds: heldTurnIds(turns) });
  const activity = entries.find((entry) => entry.item.kind === "activity");
  if (!activity || activity.item.kind !== "activity") throw new Error("no activity entry");
  const { container } = render(<Activity summary={activity.item.summary} steps={activity.item.steps} folded={false} />);
  return container.textContent ?? "";
}

describe("a call an unanswered ask holds", () => {
  it("reads as waiting to run, not running", () => {
    const text = tape([CALL, ASK(true)]);
    expect(text).toContain("Waiting to run");
    expect(text).not.toMatch(/\bRunning\b/);
  });

  it("reads as running once the ask is answered", () => {
    const text = tape([
      CALL,
      ASK(true),
      { event_type: "permission.resolved", request_id: "per_1", option_id: "allow_once" },
      { ...CALL, status: "running" },
    ]);
    expect(text).toMatch(/\bRunning\b/);
    expect(text).not.toContain("Waiting");
  });

  it("reads as running while the ask is still with the policy, not a person", () => {
    const text = tape([CALL, ASK(false)]);
    expect(text).toMatch(/\bRunning\b/);
    expect(text).not.toContain("Waiting");
  });
});
