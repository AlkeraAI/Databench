// The plan approval card's heading is the ASK, never the plan document's own
// title: a document heading can run any length and the document already shows
// it above the dock.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlanApprovalCard, type PlanModeChoice } from "@alkera/ui";

const CHOICES: PlanModeChoice[] = [
  { mode: "default", label: "Default", description: "Asks before anything destructive." },
  { mode: "auto", label: "Auto", description: "Runs reads and writes without asking." },
];

const PLAN_MARKDOWN = ["# Migrate the orders mart", "", "Two steps, one rollback point."].join("\n");

function renderCard(question: string): HTMLElement {
  return render(
    <div className="chat-root">
      <PlanApprovalCard
        content={PLAN_MARKDOWN}
        question={question}
        options={CHOICES}
        planMode={CHOICES[0]}
        resolution={null}
        onResolve={() => {}}
      />
    </div>,
  ).container;
}

describe("plan approval card title", () => {
  it("heads the card with the ask, not the plan's own heading", () => {
    const container = renderCard("Start on this fixture plan?");
    const heading = container.querySelector(".chat-plan-title");
    expect(heading?.textContent).toBe("Start on this fixture plan?");
    expect(heading?.textContent).not.toContain("Migrate the orders mart");
  });

  it("labels the option group with the ask heading", () => {
    const container = renderCard("Start on this fixture plan?");
    const heading = container.querySelector(".chat-plan-title");
    const group = container.querySelector('[role="radiogroup"]');
    expect(heading?.id).toBeTruthy();
    expect(group?.getAttribute("aria-labelledby")).toBe(heading?.id);
  });
});
