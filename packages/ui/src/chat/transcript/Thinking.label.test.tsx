// What the reasoning disclosure says about itself, and when.
//
// While the thought is arriving the block holds open and reads "Thinking…" —
// the reader watches it land, and there is nothing to toggle because there is
// nothing settled to fold away. The moment it is finished the label becomes
// the fact ("Thought for 4s"), the block folds, and the words are still one
// click away. Getting the second half wrong is what leaves a finished turn
// reading as three simultaneous in-progress thoughts.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Thinking } from "./Thinking";

const BODY = ["The table is in the staging schema.", "So I should read the file first."];

describe("the reasoning disclosure", () => {
  it("reads as in progress and holds its words open while the thought arrives", () => {
    const { container } = render(<Thinking duration="4s" body={BODY} defaultOpen streaming />);
    const toggle = screen.getByRole("button");
    expect(toggle).toHaveTextContent("Thinking…");
    expect(toggle).not.toHaveTextContent("Thought for");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    // Arriving words are eased in a span at a time, so the paragraph is read
    // off the block rather than matched as one node.
    expect(container.textContent).toContain(BODY[0]);
  });

  it("names the time it took once it is finished, and folds away", () => {
    render(<Thinking duration="4s" body={BODY} defaultOpen={false} />);
    const toggle = screen.getByRole("button");
    expect(toggle).toHaveTextContent("Thought for 4s");
    expect(toggle).not.toHaveTextContent("Thinking…");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(BODY[0])).not.toBeInTheDocument();
  });

  it("gives the finished words back to whoever asks for them", async () => {
    const user = userEvent.setup();
    render(<Thinking duration="4s" body={BODY} defaultOpen={false} />);
    await user.click(screen.getByRole("button"));
    expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(BODY[1])).toBeInTheDocument();
  });

  it("has nothing to fold while it is still thinking", () => {
    render(<Thinking duration="4s" body={BODY} defaultOpen streaming />);
    expect(screen.getByRole("button")).toBeDisabled();
  });
});
