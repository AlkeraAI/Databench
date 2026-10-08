// The agent's notebook calls read as one line each: the notebook already shows
// every change, so the transcript names what was done and where, and nothing
// more (no field table, nothing to expand).

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolConversationPart } from "@alkera/chat-model";

import { Activity } from "../activity";
import { resolveStep } from "./steps";

function call(name: string, input: Record<string, unknown>, output: Record<string, unknown>): ToolConversationPart {
  return {
    type: "tool",
    id: `call-${name}`,
    name: `alkera_${name}`,
    toolKind: "tool",
    state: "completed",
    input,
    output: JSON.stringify(output),
  } as unknown as ToolConversationPart;
}

const RUN = call(
  "notebook.run",
  { path: "work/new-notebook.alknb.py", target: { kind: "cells", ids: ["a", "b", "c", "d"] } },
  {
    path: "work/new-notebook.alknb.py",
    run_id: "run_3c5b",
    status: "finished",
    reactivity: "autorun",
    env: { env_id: "static", kind: "venv", python: "3.13.13" },
    plan: [{ cell_id: "a" }, { cell_id: "b" }, { cell_id: "c" }, { cell_id: "d" }],
  },
);

describe("a notebook call in the transcript", () => {
  it("is one line naming the action, the notebook and a short count", () => {
    const step = resolveStep(RUN);
    expect(step).not.toBeNull();
    const { container } = render(<Activity summary="1 tool call" steps={[step!]} folded={false} />);
    expect(container.textContent).toContain("Ran");
    expect(container.textContent).toContain("new-notebook.alknb.py");
    expect(container.textContent).toContain("4 cells");
    expect(container.textContent).toContain("finished");
  });

  it("carries none of the call's fields and nothing to expand", () => {
    const step = resolveStep(RUN);
    const { container } = render(<Activity summary="1 tool call" steps={[step!]} folded={false} />);
    for (const noise of ["run_3c5b", "autorun", "venv", "3.13.13", "Reactivity", "Run ID"]) {
      expect(container.textContent).not.toContain(noise);
    }
    expect(step!.body).toBeUndefined();
    expect(screen.queryByRole("button", { name: /show|hide/i })).toBeNull();
  });

  it.each([
    ["notebook.edit", { path: "nb.alknb.py", ops: [{}, {}] }, { path: "nb.alknb.py" }, "Edited", "2 changes"],
    ["notebook.create", { path: "nb.alknb.py", cells: [{}, {}, {}] }, { path: "nb.alknb.py" }, "Created", "3 cells"],
    ["notebook.kernel", { path: "nb.alknb.py", action: "restart" }, { path: "nb.alknb.py" }, "Restarted the kernel of", ""],
  ])("says what %s did", (name, input, output, verb, count) => {
    const step = resolveStep(call(name, input, output));
    const { container } = render(<Activity summary="1 tool call" steps={[step!]} folded={false} />);
    expect(container.textContent).toContain(verb);
    expect(container.textContent).toContain("nb.alknb.py");
    if (count) expect(container.textContent).toContain(count);
    expect(step!.body).toBeUndefined();
  });
});
