// A notebook shown to read: the editor's cells and outputs with nothing that
// edits, runs or opens a menu.

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EMPTY_RUNTIME, type CellRuntime, type DocCell } from "../model/types";
import { registerDefaultOutputRenderers } from "../outputs/defaults";
import { NotebookReader } from "./NotebookReader";

registerDefaultOutputRenderers();

const TABLE_MIME = "application/vnd.alkera.table+json";

function cell(id: string, kind: string, source: string, config: DocCell["config"] = {}): DocCell {
  return { id, kind, name: "_", source, config, meta: {} };
}

const CELLS: DocCell[] = [
  cell("md", "markdown", "# Quarterly sales"),
  cell("py", "python", "frame = load()\nframe"),
  cell("sql", "sql", "SELECT region FROM sales"),
];

const RUNTIME: Record<string, CellRuntime> = {
  py: {
    ...EMPTY_RUNTIME,
    outputs: [
      {
        output_id: "py/0",
        type: "display",
        data: { [TABLE_MIME]: { schema: { fields: [{ name: "region", type: "string" }] }, data: [{ region: "west" }], total_rows: 1 } },
      },
    ],
  },
};

function show(cells: DocCell[] = CELLS, runtime: Record<string, CellRuntime> = RUNTIME) {
  return render(<NotebookReader cells={cells} runtime={runtime} output={{ theme: "light" }} now={() => 0} />);
}

describe("NotebookReader", () => {
  it("draws markdown rendered, code in its editor and a saved table", () => {
    show();
    expect(screen.getByRole("heading", { name: "Quarterly sales" })).toBeTruthy();
    const cells = screen.getAllByRole("listitem");
    expect(cells).toHaveLength(3);
    expect(cells[1]!.textContent).toContain("frame = load()");
    expect(cells[2]!.textContent).toContain("SELECT region FROM sales");
    expect(within(cells[1]!).getByRole("cell", { name: "west" })).toBeTruthy();
  });

  it("offers nothing that edits or runs", () => {
    const { container } = show();
    expect(screen.queryByRole("button", { name: "Run cell" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cell actions" })).toBeNull();
    expect(screen.queryByLabelText("Drag to move the cell")).toBeNull();
    const editors = container.querySelectorAll(".cm-content");
    expect(editors.length).toBe(2);
    editors.forEach((editor) => expect(editor.getAttribute("contenteditable")).toBe("false"));
  });

  it("shows hidden code only when the reader asks", () => {
    const { container } = show([cell("hid", "python", "secret = 42", { hide_code: true })], {});
    expect(container.textContent).not.toContain("secret = 42");
    fireEvent.click(screen.getByRole("button", { name: "Code hidden. Show code" }));
    expect(container.textContent).toContain("secret = 42");
  });
});
