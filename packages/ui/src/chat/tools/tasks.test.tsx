// The task card measures a plan. A call with no plan to measure has nothing to
// meter and nothing to fold away, so the head stays a single line rather than
// carrying a track with no segments in it and a toggle over an empty well.

import type { ReactElement } from "react";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity";
import { resolveStep } from "./steps";
import s from "./tasks.module.css";

/** The progress track, however many segments it does or does not hold. */
function meter(container: HTMLElement): Element | null {
  return container.querySelector(`.${s.meter}`);
}

let seq = 0;

/** A manage_tasks call, with a fresh id per part so the module-scope disclosure
 *  memory cannot leak one test's fold into the next. */
function tasksPart(overrides: Partial<ToolConversationPart> = {}): ToolConversationPart {
  seq += 1;
  return {
    id: `part-${seq}`,
    kind: "tool",
    callId: `call-${seq}`,
    name: "manage_tasks",
    state: "completed",
    input: { action: "list" },
    output: { tasks: [], summary: { counts: { completed: 0, in_progress: 0, pending: 0, cancelled: 0 } } },
    ...overrides,
  };
}

/** A three-task roster, as manage_tasks returns it. */
const ROSTER = [
  { id: "t1", title: "Draft the schema", status: "completed" },
  { id: "t2", title: "Wire the route", status: "in_progress" },
  { id: "t3", title: "Ship it", status: "pending", blocked: true, blocked_by: ["t2"], depends_on: ["t2"] },
];

function group(part: ToolConversationPart): ReactElement {
  const step = resolveStep(part);
  if (step === null) throw new Error("manage_tasks must resolve to a step");
  return <Activity summary="Updated the task list" steps={[step]} folded={part.state === "completed"} />;
}

function renderCall(part: ToolConversationPart): HTMLElement {
  return render(group(part)).container;
}

describe("the task card with no tasks", () => {
  it("meters nothing and folds nothing away", () => {
    const container = renderCall(tasksPart());

    expect(screen.getByText("0 tasks")).toBeInTheDocument();
    expect(meter(container)).toBeNull();
    expect(container.querySelector('[data-tool="tasks"]')).toBeNull();
    expect(screen.queryByText(/done/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^(Hide|Show) tasks$/)).not.toBeInTheDocument();
  });

  it("meters nothing while the call is still in flight", () => {
    const container = renderCall(tasksPart({ state: "running", output: null }));

    expect(meter(container)).toBeNull();
    expect(screen.queryByText(/done/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^(Hide|Show) tasks$/)).not.toBeInTheDocument();
  });

  it("still meters a roster the summary counts but the payload did not carry", () => {
    const container = renderCall(
      tasksPart({ output: { tasks: [], summary: { counts: { completed: 2, in_progress: 0, pending: 1, cancelled: 0 } } } }),
    );

    expect(meter(container)?.querySelector('[data-seg="completed"]')).not.toBeNull();
    expect(screen.getByText("2 done")).toBeInTheDocument();
  });

  it("keeps a roster its summary forgot to count, but draws no track over it", () => {
    // The one payload that still reaches the meter with nothing to measure: a
    // roster the summary counts as zero. The track must stay empty of SEGMENTS,
    // not be filled with a placeholder one that reads as a stalled plan.
    const container = renderCall(
      tasksPart({
        output: { tasks: ROSTER, summary: { counts: { completed: 0, in_progress: 0, pending: 0, cancelled: 0 } } },
      }),
    );

    expect(screen.getByText("Draft the schema")).toBeInTheDocument();
    expect(meter(container)).not.toBeNull();
    expect(meter(container)?.childElementCount).toBe(0);
  });
});

describe("the task card with a roster", () => {
  it("meters the plan and folds the roster behind its word", () => {
    const container = renderCall(
      tasksPart({
        output: { tasks: ROSTER, summary: { counts: { completed: 1, in_progress: 1, pending: 1, cancelled: 0 } } },
      }),
    );

    expect(screen.getByText("3 tasks")).toBeInTheDocument();
    expect(screen.getByText("1 done, 1 blocked")).toBeInTheDocument();
    expect(screen.getByText("Hide tasks")).toBeInTheDocument();
    expect(container.querySelector('[data-seg="completed"]')).not.toBeNull();
    expect(container.querySelector('[data-seg="in_progress"]')).not.toBeNull();
    expect(screen.getByText("Draft the schema")).toBeInTheDocument();
    expect(screen.getByText("waits on t2")).toBeInTheDocument();
  });

  it("collapses to the single line when the same call comes back cleared", () => {
    // manage_tasks clears the plan under the call that wrote it, so the step
    // keeps its id across the update -- and must not keep the meter, the
    // figure, or the toggle that the roster it no longer has had earned.
    const part = tasksPart({
      output: { tasks: ROSTER, summary: { counts: { completed: 1, in_progress: 1, pending: 1, cancelled: 0 } } },
    });
    const view = render(group(part));
    expect(screen.getByText("Hide tasks")).toBeInTheDocument();

    view.rerender(
      group({ ...part, output: { tasks: [], summary: { counts: { completed: 0, in_progress: 0, pending: 0, cancelled: 0 } } } }),
    );

    expect(screen.getByText("0 tasks")).toBeInTheDocument();
    expect(meter(view.container)).toBeNull();
    expect(view.container.querySelector('[data-tool="tasks"]')).toBeNull();
    expect(screen.queryByText(/done/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^(Hide|Show) tasks$/)).not.toBeInTheDocument();
    expect(screen.queryByText("Draft the schema")).not.toBeInTheDocument();
  });
});
