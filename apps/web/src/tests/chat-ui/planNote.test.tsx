// The plan card's note goes with whichever answer is pressed, and the card's
// keys keep working from inside the field: Enter approves, Shift+Enter breaks
// the line, Escape rejects.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlanApprovalCard, type PlanModeChoice, type PlanResolution } from "@alkera/ui";

const CHOICES: PlanModeChoice[] = [
  { mode: "default", label: "Default", description: "Asks before anything destructive." },
  { mode: "auto", label: "Auto", description: "Runs reads and writes without asking." },
];

function renderCard(resolution: PlanResolution | null = null) {
  const onResolve = vi.fn<(next: PlanResolution) => void>();
  render(
    <div className="chat-root">
      <PlanApprovalCard
        content="# Plan\n\nStep one."
        question="Approve this plan?"
        options={CHOICES}
        planMode={CHOICES[0]!}
        resolution={resolution}
        onResolve={onResolve}
      />
    </div>,
  );
  return onResolve;
}

const field = (): HTMLTextAreaElement =>
  screen.getByRole("textbox", { name: "Add a note for the model (optional)" });

afterEach(cleanup);

describe("plan card note", () => {
  it("offers the field with its placeholder", () => {
    renderCard();
    expect(field().placeholder).toBe("Add a note for the model (optional)");
  });

  it("goes with the approval, trimmed", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "  Skip the migration. ");
    await userEvent.click(screen.getByRole("button", { name: /approve & start/i }));
    expect(onResolve).toHaveBeenCalledWith({ kind: "approved", mode: CHOICES[0], note: "Skip the migration." });
  });

  it("goes with the rejection", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "Split step two.");
    await userEvent.click(screen.getByRole("button", { name: /reject plan/i }));
    expect(onResolve).toHaveBeenCalledWith({ kind: "rejected", note: "Split step two." });
  });

  it("sends no note from a blank field", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "   ");
    await userEvent.click(screen.getByRole("button", { name: /approve & start/i }));
    expect(onResolve).toHaveBeenCalledWith({ kind: "approved", mode: CHOICES[0] });
    expect(onResolve.mock.calls[0]?.[0]).not.toHaveProperty("note");
  });

  it("approves on Enter from inside the field, with the note", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "Go ahead{Enter}");
    expect(onResolve).toHaveBeenCalledTimes(1);
    expect(onResolve).toHaveBeenCalledWith({ kind: "approved", mode: CHOICES[0], note: "Go ahead" });
  });

  it("breaks the line on Shift+Enter without answering", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "one{Shift>}{Enter}{/Shift}two");
    expect(onResolve).not.toHaveBeenCalled();
    expect(field().value).toBe("one\ntwo");
  });

  it("rejects on Escape from inside the field, with the note", async () => {
    const onResolve = renderCard();
    await userEvent.type(field(), "Not yet{Escape}");
    expect(onResolve).toHaveBeenCalledWith({ kind: "rejected", note: "Not yet" });
  });

  it("shows the note under the answered card", () => {
    renderCard({ kind: "approved", mode: CHOICES[1]!, note: "Skip the migration." });
    expect(screen.getByText("Approved with a note: Skip the migration.")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
