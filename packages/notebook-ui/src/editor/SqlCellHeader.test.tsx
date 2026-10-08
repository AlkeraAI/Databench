// A SQL cell's header settings, driven through the editor on the in-memory
// document: the result name, whether the result shows, and the host's
// connection control. Every assertion reads the document the editor wrote
// (the same `set_meta` the platform's live document applies).

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ABSENT_KERNEL } from "../model/types";
import { MemoryNotebook } from "./memoryHost";
import { NotebookEditor, type NotebookEditorProps } from "./NotebookEditor";
import type { SqlConnectionSlot } from "./ports";

function setup(opts: { meta?: Record<string, unknown>; canEdit?: boolean; sqlConnection?: NotebookEditorProps["sqlConnection"] } = {}) {
  const doc = new MemoryNotebook([
    { id: "q1", kind: "sql", name: "revenue", source: "SELECT 1", meta: { output_var: "_df", ...opts.meta } },
    { id: "p1", source: "x = 1" },
  ]);
  render(
    <NotebookEditor
      doc={doc}
      text={doc}
      runtime={{
        cells: {},
        kernel: { ...ABSENT_KERNEL, state: "idle" },
        graph: { edges: [], errors: {} },
        presence: [],
        confirmation: null,
        envs: [],
        packages: [],
        installing: false,
        notice: null,
      }}
      actions={{ run: () => {}, confirmRun: () => {}, cancelRun: () => {}, kernel: () => {}, switchEnv: () => {}, install: () => {} }}
      permissions={{ canEdit: opts.canEdit ?? true, canRun: opts.canEdit ?? true }}
      output={{ theme: "light" }}
      platform="other"
      {...(opts.sqlConnection ? { sqlConnection: opts.sqlConnection } : {})}
    />,
  );
  const cell = (id: string) => document.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
  const meta = () => doc.snapshot().cells.find((c) => c.id === "q1")!.meta;
  return { doc, cell, meta };
}

describe("a SQL cell's result name", () => {
  it("renames the result on Enter", () => {
    const { cell, meta } = setup();
    const input = within(cell("q1")).getByRole("textbox", { name: "Result name" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "orders" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    expect(meta().output_var).toBe("orders");
  });

  it.each([
    ["starts with a digit", "1orders"],
    ["is a keyword", "class"],
    ["has a space", "my df"],
    ["is empty", ""],
  ])("refuses a name that %s and keeps the old one", (_why, name) => {
    const { cell, meta } = setup();
    const input = within(cell("q1")).getByRole("textbox", { name: "Result name" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: name } });
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(within(cell("q1")).getByRole("alert")).toHaveTextContent("Not a valid Python name");
    fireEvent.blur(input);
    expect(meta().output_var).toBe("_df");
    expect(input).toHaveValue("_df");
  });

  it("Escape puts the name back without writing", () => {
    const { cell, meta } = setup();
    const input = within(cell("q1")).getByRole("textbox", { name: "Result name" });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "orders" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue("_df");
    fireEvent.blur(input);
    expect(meta().output_var).toBe("_df");
  });

  it("follows a rename someone else makes", () => {
    const { doc, cell } = setup();
    act(() => void doc.apply([{ op: "set_meta", cell_id: "q1", meta: { output_var: "revenue" } }]));
    expect(within(cell("q1")).getByRole("textbox", { name: "Result name" })).toHaveValue("revenue");
  });
});

describe("whether a SQL cell's result shows", () => {
  it("is a cell menu command, not a control in the header", () => {
    const { cell, meta } = setup();
    expect(within(cell("q1")).queryByRole("checkbox", { name: "Show result" })).toBeNull();
    fireEvent.click(within(cell("q1")).getByRole("button", { name: "Cell actions" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Show result" }));
    expect(meta().show_output).toBe(false);
    expect(within(cell("q1")).getByText("Result hidden")).toBeInTheDocument();
    fireEvent.click(within(cell("q1")).getByRole("button", { name: "Cell actions" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Show result" }));
    expect(meta().show_output).toBe(true);
  });

  it("is offered only on SQL cells", () => {
    const { cell } = setup();
    fireEvent.click(within(cell("p1")).getByRole("button", { name: "Cell actions" }));
    expect(screen.queryByRole("menuitemcheckbox", { name: "Show result" })).toBeNull();
  });
});

describe("a SQL cell's connection", () => {
  it("draws the host's control and writes what it picks", () => {
    const slots: SqlConnectionSlot[] = [];
    const { cell, meta } = setup({
      meta: { connection: "warehouse" },
      sqlConnection: (slot) => {
        slots.push(slot);
        return (
          <button type="button" onClick={() => slot.onChange(null)}>
            Pick DuckDB
          </button>
        );
      },
    });
    // Only the SQL cell gets the control, told what it stores and that it may change it.
    expect(within(cell("p1")).queryByRole("button", { name: "Pick DuckDB" })).toBeNull();
    expect(slots.at(-1)).toMatchObject({ cellId: "q1", connection: "warehouse", canEdit: true });
    fireEvent.click(within(cell("q1")).getByRole("button", { name: "Pick DuckDB" }));
    expect(meta().connection).toBeUndefined();
  });

  it("shows the name when the host has no control", () => {
    const { cell } = setup({ meta: { connection: "warehouse" } });
    expect(within(cell("q1")).getByTitle("Connection")).toHaveTextContent("warehouse");
  });
});

describe("a reader", () => {
  it("sees the result name and whether it is hidden, with no controls", () => {
    const { cell } = setup({ meta: { show_output: false }, canEdit: false });
    expect(within(cell("q1")).queryByRole("textbox", { name: "Result name" })).toBeNull();
    expect(within(cell("q1")).queryByRole("checkbox", { name: "Show result" })).toBeNull();
    expect(within(cell("q1")).getByTitle("Result name")).toHaveTextContent("_df");
    expect(screen.getByText("Result hidden")).toBeInTheDocument();
  });
});

describe("a cell's title", () => {
  it("leads with the cell's name and puts the quiet kind after it", () => {
    const { cell } = setup();
    const header = cell("q1").querySelector(".nb-cell__header")!;
    const order = [...header.querySelectorAll(".nb-cell__name, .nb-cell__kind")].map((el) => el.textContent);
    expect(order).toEqual(["revenue", "Cell 1 · SQL"]);
    // An unnamed cell still says which cell it is.
    const second = cell("p1").querySelector(".nb-cell__header")!;
    expect(second.querySelector(".nb-cell__name")).toBeNull();
    expect(second.querySelector(".nb-cell__kind")).toHaveTextContent("Cell 2 · Python");
  });
});
