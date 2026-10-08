// The run-level fold is a default, not a veto: a step whose payload declares
// itself open (a query result that IS the answer) stays readable once the turn
// settles, while a run of unbounded output (bash, grep) still folds away.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Activity } from "./Activity";
import type { CardStep } from "../tools";

let seq = 0;

/** A fresh id per step keeps the module-scope disclosure memory from leaking
 *  one test's reader-fold into the next. */
function step(overrides: Partial<CardStep> = {}): CardStep {
  seq += 1;
  return {
    id: `step-${seq}`,
    verb: "Queried",
    object: "tinybird",
    objectKind: "pattern",
    status: "done",
    glyph: null,
    loneSummary: "Ran 1 query",
    body: <div data-testid={`well-${seq}`}>7 rows · 156 ms</div>,
    ...overrides,
  };
}

describe("Activity", () => {
  it("keeps a settled run open when a step declares itself expanded", () => {
    const result = step({ expanded: true, body: <div data-testid="sql-well">SELECT 1</div> });
    render(<Activity summary="Ran 1 query" steps={[result]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 query/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("sql-well")).toBeInTheDocument();
  });

  it("folds a settled run whose steps declare no openness", () => {
    const shell = step({ verb: "Ran", object: "ls -la", objectKind: "command", loneSummary: "Ran 1 command", body: <div data-testid="bash-well">out</div> });
    render(<Activity summary="Ran 1 command" steps={[shell]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 command/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("bash-well")).not.toBeInTheDocument();
  });

  it("does not fold an open step on the live -> settled transition", () => {
    const running = step({ expanded: true, status: "running", body: <div data-testid="live-well">rows</div> });
    const view = render(<Activity summary="Ran 1 query" steps={[running]} folded={false} />);

    view.rerender(<Activity summary="Ran 1 query" steps={[{ ...running, status: "done" }]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 query/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("live-well")).toBeInTheDocument();
  });

  it("still folds a settled bash run on the transition", () => {
    const running = step({ verb: "Ran", object: "pytest", objectKind: "command", status: "running", loneSummary: "Ran 1 command", body: <div data-testid="bash-live">out</div> });
    const view = render(<Activity summary="Ran 1 command" steps={[running]} folded={false} />);
    expect(screen.getByRole("button", { name: /Ran 1 command/ })).toHaveAttribute("aria-expanded", "true");

    view.rerender(<Activity summary="Ran 1 command" steps={[{ ...running, status: "done" }]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 command/ })).toHaveAttribute("aria-expanded", "false");
  });

  // A call whose arguments are still streaming has not answered either. It takes
  // the same loading sweep a running one does -- never a disclosure over a body
  // built from a payload that has not arrived. These fail if `pending` drops out
  // of the in-flight branch.
  for (const status of ["pending", "running"] as const) {
    it(`draws a ${status} step as work in progress, not as an answer`, () => {
      const live = step({ status, body: <div data-testid={`inflight-${status}`}>rows</div> });
      const { container } = render(<Activity summary="Ran 1 query" steps={[live]} folded={false} />);

      expect(container.querySelector(".chat-activity-well__work")).toBeInTheDocument();
      expect(screen.queryByText(/Show details/)).not.toBeInTheDocument();
      expect(screen.queryByTestId(`inflight-${status}`)).not.toBeInTheDocument();
    });
  }

  it("keeps a mixed run open when any step declares itself expanded", () => {
    const shell = step({ verb: "Ran", object: "ls", objectKind: "command", loneSummary: "Ran 1 command" });
    const result = step({ expanded: true, body: <div data-testid="mixed-well">rows</div> });
    render(<Activity summary="Ran 2 tools" steps={[shell, result]} folded />);

    expect(screen.getByRole("button", { name: /Ran 2 tools/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("mixed-well")).toBeInTheDocument();
  });
});

describe("Activity: a refused or failed command shows its reason", () => {
  const reason = "The workspace policy refused this read: /etc/passwd is outside this chat's workspace";

  it("keeps a settled run open and reads the reason under the head, with the well still shut", () => {
    const refused = step({
      verb: "Ran",
      object: "cat /etc/passwd",
      objectKind: "command",
      status: "error",
      loneSummary: "Ran 1 terminal command",
      failure: reason,
      body: <div data-testid="refused-well">$ cat /etc/passwd</div>,
    });
    render(<Activity summary="Ran 1 terminal command" steps={[refused]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 terminal command/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.queryByTestId("refused-well")).not.toBeInTheDocument();
  });

  it("does not fold the run away on the live -> settled transition", () => {
    const running = step({
      verb: "Ran",
      object: "python -c 'print(1)'",
      objectKind: "command",
      status: "running",
      loneSummary: "Ran 1 terminal command",
      body: <div>$ python</div>,
    });
    const view = render(<Activity summary="Ran 1 terminal command" steps={[running]} folded={false} />);

    view.rerender(<Activity summary="Ran 1 terminal command" steps={[{ ...running, status: "error", failure: reason }]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 terminal command/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("the control: a failure with no reason still folds like any settled shell run", () => {
    const failed = step({
      verb: "Ran",
      object: "false",
      objectKind: "command",
      status: "error",
      loneSummary: "Ran 1 terminal command",
      body: <div data-testid="plain-well">$ false</div>,
    });
    render(<Activity summary="Ran 1 terminal command" steps={[failed]} folded />);

    expect(screen.getByRole("button", { name: /Ran 1 terminal command/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(screen.queryByTestId("plain-well")).not.toBeInTheDocument();
  });
});

describe("Activity failure line", () => {
  const pgError =
    'relation "missing" does not exist\nLINE 1: SELECT * FROM missing LIMIT 5\n                      ^';

  it("keeps a multi-line cause on its own lines, the caret on its own row", () => {
    const failed = step({ status: "error", failure: pgError, body: undefined });
    const { container } = render(<Activity summary="Ran 1 query" steps={[failed]} folded />);

    const text = container.querySelector(".chat-tool-fail__text");
    expect(text).not.toBeNull();
    expect(text?.textContent).toBe(pgError);
    expect(text?.textContent?.split("\n")).toHaveLength(3);
    expect(text).toHaveClass("chat-tool-fail__text--lines");
  });

  it("the control: a one-line cause sets as plain prose", () => {
    const failed = step({ status: "error", failure: "permission denied for table orders", body: undefined });
    const { container } = render(<Activity summary="Ran 1 query" steps={[failed]} folded />);

    const text = container.querySelector(".chat-tool-fail__text");
    expect(text?.textContent).toBe("permission denied for table orders");
    expect(text).not.toHaveClass("chat-tool-fail__text--lines");
  });
});

describe("Activity: a refusal is counted apart from a failure", () => {
  const refusal = step({
    verb: "Refused",
    object: "rm -rf ./scratch",
    objectKind: "command",
    status: "error",
    refused: true,
    loneSummary: "Refused 1 terminal command",
    failure: "Permission was not granted for this call.",
  });

  it("a lone refused call carries no failure count: its head already says it", () => {
    render(<Activity summary="Refused 1 terminal command" steps={[refusal]} folded />);
    expect(screen.queryByText(/\bfailed\b/)).toBeNull();
    expect(screen.queryByText("1 refused")).toBeNull();
    expect(screen.getByText("Permission was not granted for this call.")).toBeInTheDocument();
  });

  it("a run with a refusal and a failure counts each apart", () => {
    const failed = step({
      verb: "Ran",
      object: "false",
      objectKind: "command",
      status: "error",
      loneSummary: "Ran 1 terminal command",
      failure: "exit status 1",
    });
    render(<Activity summary="Ran 2 terminal commands" steps={[refusal, failed]} folded />);
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(screen.getByText("1 refused")).toBeInTheDocument();
  });

  it("a run of several with one refusal says refused, never failed", () => {
    const ran = step({
      verb: "Ran",
      object: "ls",
      objectKind: "command",
      status: "done",
      loneSummary: "Ran 1 terminal command",
    });
    render(<Activity summary="Ran 2 terminal commands" steps={[ran, refusal]} folded />);
    expect(screen.queryByText(/\bfailed\b/)).toBeNull();
    expect(screen.getByText("1 refused")).toBeInTheDocument();
  });

  it("a mixed run's head counts only the calls that ran", () => {
    const ran = step({ verb: "Ran", object: "ls", objectKind: "command", loneSummary: "Ran 1 terminal command" });
    const { container } = render(<Activity summary="Ran 2 tools" steps={[ran, refusal]} folded />);
    expect(container.querySelector(".chat-activity-digest__stat")?.textContent).toBe("Ran 1 tool");
    expect(screen.getByText("1 refused")).toBeInTheDocument();
  });

  it("a run where every call was refused says so in its head, with no separate count", () => {
    const second = step({ ...refusal, id: "refused-second" });
    const { container } = render(<Activity summary="Ran 2 tools" steps={[refusal, second]} folded />);
    expect(container.querySelector(".chat-activity-digest__stat")?.textContent).toBe("Refused 2 tool calls");
    expect(screen.queryByText("2 refused")).toBeNull();
    expect(screen.queryByText(/^Ran\b/)).toBeNull();
  });

  it("the control: a run with nothing refused keeps its summary", () => {
    const one = step({ loneSummary: "Ran 1 terminal command" });
    const two = step({ loneSummary: "Ran 1 terminal command" });
    const { container } = render(<Activity summary="Ran 2 tools" steps={[one, two]} folded />);
    expect(container.querySelector(".chat-activity-digest__stat")?.textContent).toBe("Ran 2 tools");
  });
});
