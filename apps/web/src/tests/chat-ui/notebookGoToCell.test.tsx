// "Go to cell" on a notebook step: the result names the notebook by its path in
// the workspace and the cell by its stable id, the card offers the action where
// the shell can open a notebook, and the action is its own button beside the
// disclosure, never the head's toggle.

import { fireEvent, render, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Activity } from "@alkera/ui";

import { renderBody, stepOf, toolPart } from "./_steps";

const BOX_PATH = "/home/alkera/new-notebook.alknb.py";
const WORKSPACE_PATH = "new-notebook.alknb.py";
const CELL = "a7yg9x7evz";

const data = (content: unknown) => ({ untrusted: true, author: "Agent", content });

const SHOWN = toolPart("alkera_notebook.show_output", {
  input: { path: BOX_PATH, cell: "visibility_roi_chart" },
  output: JSON.stringify({
    path: WORKSPACE_PATH,
    cell_id: CELL,
    cell_name: "visibility_roi_chart",
    kind: "text",
    available: ["text"],
    text: data("0.42"),
    note: "",
  }),
});

describe("going to the cell a notebook step names", () => {
  it("sits at the right of the output's file bar and opens the cell by its stable id", () => {
    const onOpenNotebookCell = vi.fn();
    const step = stepOf(SHOWN, { onOpenNotebookCell });
    const { container } = render(
      <div className="chat-root">
        <Activity steps={[step]} summary="Ran 1 tool" />
      </div>,
    );
    const go = within(container).getByRole("button", { name: "Go to cell visibility_roi_chart" });
    expect(go.textContent).toBe("Go to cell");
    const bar = go.closest(".chat-tool-band-row");
    expect(bar?.textContent).toContain(WORKSPACE_PATH);
    expect(bar?.lastElementChild).toBe(go);

    fireEvent.click(go);
    expect(onOpenNotebookCell).toHaveBeenCalledWith(WORKSPACE_PATH, CELL);
  });

  it("is a button beside the disclosure that does not fold the output", () => {
    const onOpenNotebookCell = vi.fn();
    const step = stepOf(SHOWN, { onOpenNotebookCell });
    const { container } = render(
      <div className="chat-root">
        <Activity steps={[step]} summary="Ran 1 tool" />
      </div>,
    );
    const head = container.querySelector<HTMLElement>("button.chat-activity-head")!;
    expect(head.textContent).toContain("Hide output");
    const go = within(container).getByRole("button", { name: "Go to cell visibility_roi_chart" });
    expect(head.contains(go)).toBe(false);

    fireEvent.click(go);

    expect(onOpenNotebookCell).toHaveBeenCalledWith(WORKSPACE_PATH, CELL);
    expect(head.getAttribute("aria-expanded")).toBe("true");
  });

  it("names the notebook by its workspace path, not the machine's", () => {
    const body = renderBody(stepOf(SHOWN));
    expect(body.textContent).toContain(WORKSPACE_PATH);
    expect(body.textContent).not.toContain("/home/alkera");
  });

  it.each([
    ["a shell that cannot open a notebook", SHOWN, undefined],
    [
      "a cell named but not by a stable id",
      toolPart("alkera_notebook.show_output", {
        input: { path: WORKSPACE_PATH },
        output: JSON.stringify({ path: WORKSPACE_PATH, cell_id: "Cell 3", cell_name: "x", kind: "text", text: data("1") }),
      }),
      vi.fn(),
    ],
    [
      "a call that failed",
      toolPart("alkera_notebook.show_output", { state: "error", input: { path: WORKSPACE_PATH, cell: CELL } }),
      vi.fn(),
    ],
  ])("is not offered for %s", (_case, part, onOpenNotebookCell) => {
    expect(stepOf(part, { onOpenNotebookCell }).action).toBeUndefined();
  });

  it("is offered by a notebook.output call and a read of one cell, not by a read of many", () => {
    const onOpenNotebookCell = vi.fn();
    const output = toolPart("alkera_notebook.output", {
      input: { path: BOX_PATH, cell: "last" },
      output: JSON.stringify({ path: WORKSPACE_PATH, cell_id: CELL }),
    });
    stepOf(output, { onOpenNotebookCell }).action?.onAction();
    expect(onOpenNotebookCell).toHaveBeenLastCalledWith(WORKSPACE_PATH, CELL);

    const one = toolPart("alkera_notebook.read", {
      input: { path: BOX_PATH, cells: ["revenue"] },
      output: JSON.stringify({ path: WORKSPACE_PATH, cells: [{ id: "bbbbbbbbbb", name: "revenue" }] }),
    });
    const action = stepOf(one, { onOpenNotebookCell }).action;
    expect(action?.ariaLabel).toBe("Go to cell revenue");
    action?.onAction();
    expect(onOpenNotebookCell).toHaveBeenLastCalledWith(WORKSPACE_PATH, "bbbbbbbbbb");

    const many = toolPart("alkera_notebook.read", {
      input: { path: BOX_PATH },
      output: JSON.stringify({ path: WORKSPACE_PATH, cells: [{ id: "bbbbbbbbbb", name: "revenue" }, { id: CELL, name: "x" }] }),
    });
    expect(stepOf(many, { onOpenNotebookCell }).action).toBeUndefined();
  });
});
