// A tool search is the agent finding its own tools: it says nothing the reader
// acts on, so its card starts folded and the list opens only on request.

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity";
import { resolveStep } from "./steps";

const search: ToolConversationPart = {
  type: "tool",
  id: "call-1",
  name: "search_tools",
  toolKind: "tool",
  state: "completed",
  input: { query: "notebook edit run cell" },
  output: JSON.stringify({
    tools: [{ name: "notebook.edit", description: "Change a notebook's cells.", plugin: "notebook", params: [] }],
  }),
} as unknown as ToolConversationPart;

describe("a tool search card", () => {
  it("starts folded, and opens on request", () => {
    const step = resolveStep(search);
    expect(step).not.toBeNull();
    render(<Activity summary="Searched tools" steps={[step!]} />);
    expect(screen.queryByText("notebook.edit")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /tools/i }));
    expect(screen.getByText("notebook.edit")).toBeInTheDocument();
  });
});
