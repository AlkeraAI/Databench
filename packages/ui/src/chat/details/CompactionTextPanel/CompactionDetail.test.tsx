import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CompactionConversationPart } from "@alkera/chat-model";
import { CompactionTextPanel } from "./CompactionDetail";

describe("CompactionTextPanel", () => {
  it("renders the part's text as Markdown", () => {
    // Pins the field name at the type level: the fold's part carries `text`, not `summary`.
    const part: CompactionConversationPart = {
      id: "c1",
      kind: "compaction",
      text: "## What was compacted\n\nThe first 40 turns.",
    };
    render(<CompactionTextPanel text={part.text} />);
    expect(screen.getByRole("heading", { level: 2, name: "What was compacted" })).toBeInTheDocument();
    expect(screen.getByText("The first 40 turns.")).toBeInTheDocument();
  });
});
