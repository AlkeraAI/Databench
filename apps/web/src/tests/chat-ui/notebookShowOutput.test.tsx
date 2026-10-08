// A notebook output the agent put in the chat, drawn by the card.
//
// Every other notebook call is one quiet line; `notebook.show_output` is the
// one whose result a reader came for, so each kind is asserted as what the
// reader sees: the table's cells in a grid, the chart's marks drawn from the
// spec's own rows (an image read by its hash is `notebookShowImage.test.tsx`).
// The payloads are the tool's result shape (`NotebookShowOutputOutput`), with
// the notebook's own values inside the wrapper that marks them as data.

import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

const PATH = "work/analysis.alknb.py";

const data = (content: unknown) => ({ untrusted: true, author: "Agent", content });

function shown(over: Record<string, unknown>, input: Record<string, unknown> = {}) {
  return toolPart("alkera_notebook.show_output", {
    input: { path: PATH, ...input },
    output: JSON.stringify({ path: PATH, cell_id: "a7yg9x7evz", available: [over.kind], note: "", ...over }),
  });
}

const TABLE = shown({
  kind: "table",
  cell_name: "orders_table",
  table: {
    columns: ["n", "label"],
    rows: data([
      [0, "r0"],
      [1, "r1"],
      [2, "r2"],
    ]),
    shown_rows: 3,
    total_rows: 500,
    total_columns: 2,
  },
  note: "Showing 3 of 500 rows. The whole table is stored with this result.",
  blob: { sha256: "h0", size: 500, media_type: "rows" },
  ref_type: "rows",
  result_name: "orders_table, 500 rows",
});

const CHART = shown({
  kind: "chart",
  cell_name: "by_region",
  chart_spec: data({
    $schema: "https://vega.github.io/schema/vega-lite/v6.json",
    mark: "bar",
    encoding: {
      x: { field: "region", type: "nominal" },
      y: { field: "revenue", type: "quantitative" },
    },
    data: { name: "sales" },
    datasets: {
      sales: [
        { region: "north", revenue: 3 },
        { region: "south", revenue: 4 },
        { region: "west", revenue: 5 },
      ],
    },
  }),
});

describe("a notebook output shown in the chat", () => {
  it("draws a table in the result grid and says how much of it is shown", () => {
    const step = stepOf(TABLE);
    expect(step.verb).toBe("Showed");
    expect(step.object).toBe("orders_table");
    expect(step.data).toEqual({ kind: "count", text: "3 of 500 rows" });
    expect(step.expanded).toBe(true);

    const body = renderBody(step);
    const grid = within(body).getByRole("table", { name: "orders_table table" });
    expect(within(grid).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["", "n", "label"]);
    const cells = within(grid).getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toEqual(["0", "r0", "1", "r1", "2", "r2"]);
    // The all-numeric column is right-aligned as a SQL result's is.
    expect(within(grid).getAllByRole("cell")[0].hasAttribute("data-num")).toBe(true);
    expect(within(grid).getAllByRole("cell")[1].hasAttribute("data-num")).toBe(false);
    expect(body.textContent).toContain("Showing 3 of 500 rows. The whole table is stored with this result.");
  });

  it("draws a chart from the spec's own rows, one bar per row", async () => {
    const step = stepOf(CHART);
    expect(step.object).toBe("by_region");
    expect(step.data).toEqual({ kind: "count", text: "chart" });

    const body = renderBody(step);
    await waitFor(() => expect(body.querySelectorAll(".mark-rect path")).toHaveLength(3));
    expect(within(body).getByRole("figure", { name: "by_region chart" })).toBeTruthy();
  });


  it("renders Markdown as prose, not as its source", () => {
    const part = shown({ kind: "markdown", cell_name: "intro", markdown: data("# Revenue\n\nBy *region*.") });
    const body = renderBody(stepOf(part));

    expect(within(body).getByRole("heading", { name: "Revenue" })).toBeTruthy();
    expect(body.querySelector("em")?.textContent).toBe("region");
    expect(body.textContent).not.toContain("# Revenue");
  });

  it("shows the cell's error as the output, on a call that itself succeeded", () => {
    const part = shown({
      kind: "error",
      cell_name: "broken",
      cell_error: { ename: "ZeroDivisionError", evalue: data("division by zero"), traceback: data("line 1\n1 / 0") },
    });
    const step = stepOf(part);
    expect(step.status).toBe("done");

    const body = renderBody(step);
    const error = within(body).getByRole("group", { name: "broken error" });
    expect(error.textContent).toContain("ZeroDivisionError: division by zero");
    expect(error.textContent).toContain("1 / 0");
  });

  it("opens the notebook by the path the agent named, only where the shell can", () => {
    const onOpenPath = vi.fn();
    renderBody(stepOf(TABLE, { onOpenPath }));
    screen.getByRole("button", { name: `Open ${PATH}` }).click();
    expect(onOpenPath).toHaveBeenCalledWith(PATH);

    const plain = renderBody(stepOf(TABLE));
    expect(within(plain).queryByRole("button", { name: `Open ${PATH}` })).toBeNull();
  });

  it("ignores a value that is not marked as the notebook's data", () => {
    // A table whose rows arrive bare (no wrapper) is not what the tool sends;
    // the card draws the columns and no rows rather than trusting the shape.
    const part = shown({
      kind: "table",
      cell_name: "odd",
      table: { columns: ["n"], rows: [[1]], shown_rows: 1, total_rows: 1, total_columns: 1 },
    });
    const body = renderBody(stepOf(part));
    const grid = within(body).getByRole("table", { name: "odd table" });
    expect(within(grid).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["", "n"]);
    expect(within(grid).queryAllByRole("cell")).toHaveLength(0);
  });
});
