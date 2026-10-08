// The note card's visibility chip is a label, so it is sentence case like every
// other label: "Shared" and "Private", never the store's lowercase word.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity";
import { resolveStep } from "./steps";

function noteChip(visibility: string): string {
  const part: ToolConversationPart = {
    id: `vis-${visibility}`,
    kind: "tool",
    callId: `vis-call-${visibility}`,
    name: "alkera_context_note",
    state: "completed",
    input: { title: "Walkthrough note", text: "hello", visibility },
    output: JSON.stringify({ item_id: "fact:1", title: "Walkthrough note", visibility, grade: "B" }),
  };
  const step = resolveStep(part);
  if (!step) throw new Error("no step");
  const { container } = render(<Activity summary="1 tool call" steps={[{ ...step, expanded: true }]} folded={false} />);
  return container.querySelector(".chat-context-chip[data-vis]")?.textContent ?? "";
}

describe("the note card's visibility chip", () => {
  it.each([
    ["shared", "Shared"],
    ["private", "Private"],
  ])("%s reads %s", (visibility, label) => {
    expect(noteChip(visibility)).toBe(label);
  });
});
