// A write's verb says what has happened, never what is about to. The harness
// streams a call's arguments, so a write sits PENDING with an empty input for
// the whole streaming window -- and a past-tense verb there tells the reader a
// file is already on disk while the model is still deciding its name.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart, ToolState } from "@alkera/chat-model";

import { Activity } from "../activity";
import type { CardStep } from "./step";
import { resolveStep } from "./steps";

/** A write call at one point in its life, resolved through the registry -- the
 *  one door every transcript surface reaches a card through. */
function writeStep(state: ToolState, input: Record<string, unknown> = {}): CardStep {
  const part: ToolConversationPart = {
    id: "part-1",
    kind: "tool",
    callId: "call-1",
    name: "write",
    state,
    input,
  };
  const step = resolveStep(part);
  if (!step) throw new Error("the write tool resolved to no step");
  return step;
}

/** What a settled write carries: the path the model chose and the text it put
 *  there. A call in flight has neither yet. */
const AUTHORED = { filePath: "/workspace/notes.md", content: "hello\n" };

describe("the write card's verb", () => {
  it.each([
    { state: "pending" as const, why: "the arguments are still streaming" },
    { state: "running" as const, why: "the write has not returned" },
  ])("reads in progress while the call is $state ($why)", ({ state }) => {
    // A pending call carries no input at all, which is exactly the window the
    // past-tense verb was wrong in.
    const step = writeStep(state, state === "pending" ? {} : AUTHORED);

    expect(step.verb).toBe("Writing…");
    expect(step.verb).not.toBe("Wrote");
  });

  it("reads past tense once the call has completed", () => {
    expect(writeStep("completed", AUTHORED).verb).toBe("Wrote");
  });

  it("reads as a failure on a settled failure, not as a call still in flight", () => {
    // A dead call must not keep claiming to be working, nor claim it wrote.
    expect(writeStep("error", AUTHORED).verb).toBe("Could not write");
  });

  it("shows the reader the in-progress word on a pending write, and not the past one", () => {
    render(<Activity summary="Ran 1 write" steps={[writeStep("pending")]} />);

    expect(screen.getByText("Writing…")).toBeInTheDocument();
    expect(screen.queryByText("Wrote")).not.toBeInTheDocument();
  });

  it("shows the reader the past word once the write has landed", () => {
    render(<Activity summary="Ran 1 write" steps={[writeStep("completed", AUTHORED)]} />);

    expect(screen.getByText("Wrote")).toBeInTheDocument();
    expect(screen.queryByText("Writing…")).not.toBeInTheDocument();
  });
});
