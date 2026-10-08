// A notebook chart too large for the tool's reply is drawn in the chat from
// the file the notebook stores its spec in. No size cutoff: the reply carries
// the stored file's hash, and the shell reads the spec, once the card is on
// screen, and draws it.

import { waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";
import { handScrolledViewport } from "./_viewport";

beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
});

let viewport: ReturnType<typeof handScrolledViewport>;

beforeEach(() => {
  viewport = handScrolledViewport();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const PATH = "roi.alknb.py";
const SHA = "9f".repeat(32);
const ROWS = Array.from({ length: 2000 }, (_, i) => ({ day: `d${String(i).padStart(4, "0")}`, sov_daily: i % 50 }));
const SPEC = {
  $schema: "https://vega.github.io/schema/vega-lite/v6.json",
  mark: "bar",
  encoding: { x: { field: "day", type: "nominal" }, y: { field: "sov_daily", type: "quantitative" } },
  data: { name: "daily" },
  datasets: { daily: ROWS },
};

const STORED = toolPart("alkera_notebook.show_output", {
  input: { path: PATH, cell: "visibility_roi_chart" },
  output: JSON.stringify({
    path: PATH,
    cell_id: "a7yg9x7evz",
    cell_name: "visibility_roi_chart",
    kind: "chart",
    available: ["chart"],
    chart_spec: null,
    chart_ref: { sha256: SHA, bytes: 120_000 },
    chart_summary: { untrusted: true, author: "Agent", content: "bar chart (x=day, y=sov_daily) drawing 2000 rows" },
    note: "",
  }),
});

describe("a chart too large for the tool's reply", () => {
  it("is read from where the notebook stores it, once on screen, and drawn whole", async () => {
    const notebookChartSpec = vi.fn(async () => ({ kind: "ready", value: SPEC }) as const);
    const body = renderBody(stepOf(STORED, { notebookChartSpec }));
    expect(notebookChartSpec).not.toHaveBeenCalled();

    viewport.scrollTo(body);

    expect(notebookChartSpec).toHaveBeenCalledWith(PATH, SHA);
    const figure = await within(body).findByRole("figure", { name: "visibility_roi_chart chart" });
    await waitFor(() => expect(figure.querySelectorAll(".mark-rect path")).toHaveLength(2000));
    expect(body.textContent).not.toContain("too large");
    expect(body.textContent).not.toContain("Open the notebook");
  });

  it.each([
    ["a shell with nowhere to read stored outputs", undefined, "Open the notebook to see this chart."],
    ["a reader the notebook is not open to", async () => ({ kind: "refused" }) as const, "You don't have access to this notebook."],
    [
      "a chart the cell no longer shows",
      async () => ({ kind: "gone" }) as const,
      "This chart is no longer in the notebook. The cell ran again or was deleted.",
    ],
    ["a read that fails", async () => Promise.reject(new Error("offline")), "The chart could not be loaded."],
    ["a stored file that is not a spec", async () => ({ kind: "ready", value: "not a spec" }) as const, "The chart could not be loaded."],
  ])("says why there is no chart for %s", async (_case, notebookChartSpec, note) => {
    const body = renderBody(stepOf(STORED, { notebookChartSpec }));
    viewport.scrollTo(body);
    expect(await within(body).findByText(note)).toBeTruthy();
  });
});
