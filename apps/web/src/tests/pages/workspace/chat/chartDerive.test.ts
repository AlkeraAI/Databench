// What a result offers to chart, before anyone has chosen anything.
//
// The rule the cases exist for is that the offer must be *earned* by the data:
// a chart is proposed only when the preview actually reads as a time series —
// exactly one column that is a date and at least one that is a number. A shape
// that is not a time series must produce no offer at all, because an offered
// chart the reader has to un-tick is a chart nobody asked for, and a spec built
// over columns the server would refuse is an error the reader sees instead of a
// result.
//
// The second rule is why the labelled previews below are a matrix rather than a
// case: there is no series split. A `color` channel splits one result into
// several lines — the multi-series chart the scope rules out, which the reader
// we ship cannot draw and which the server's allowlist now refuses by name. So
// a preview carrying a low-cardinality label has to produce the SAME two-channel
// spec as one without it; anything else is a save the server answers with a 422.

import { describe, expect, it } from "vitest";

import {
  chartFieldsOf,
  timeSeriesSpec,
  type PreviewCell,
} from "@/pages/workspace/chat/chartDerive";
import { guardSpec } from "@alkera/ui";

import { chartCaption } from "@/pages/workspace/objects/chartSpec";

describe("the chart a preview earns", () => {
  it("proposes the date column against the first numeric one", () => {
    const fields = chartFieldsOf(
      ["day", "orders"],
      [
        ["2026-09-01", 12],
        ["2026-09-02", 19],
      ],
    );
    expect(fields).toEqual({
      x: "day",
      xOptions: ["day", "orders"],
      y: "orders",
      yOptions: ["orders"],
      color: null,
    });
  });

  it("recognises a date column by the shape of its values, not only its name", () => {
    const fields = chartFieldsOf(
      ["bucket", "n"],
      [
        ["2026-09-01T00:00:00Z", 3],
        ["2026-09-02T00:00:00Z", 4],
      ],
    );
    expect(fields?.x).toBe("bucket");
    expect(fields?.y).toBe("n");
  });

  it("offers no chart when nothing in the preview is a date", () => {
    expect(
      chartFieldsOf(
        ["customer", "orders"],
        [
          ["acme", 12],
          ["globex", 19],
        ],
      ),
    ).toBeNull();
  });

  it("offers no chart when two columns are both dates — which one is the axis is not ours to guess", () => {
    expect(
      chartFieldsOf(["started_at", "ended_at", "n"], [["2026-09-01", "2026-09-02", 1]]),
    ).toBeNull();
  });

  it("offers no chart when the only other column is text", () => {
    expect(chartFieldsOf(["day", "engine"], [["2026-09-01", "tinybird"]])).toBeNull();
  });

  it("takes the first numeric column and leaves the rest selectable", () => {
    const fields = chartFieldsOf(
      ["day", "orders", "revenue"],
      [
        ["2026-09-01", 12, 99.5],
        ["2026-09-02", 19, 120],
      ],
    );
    expect(fields?.y).toBe("orders");
    expect(fields?.yOptions).toEqual(["orders", "revenue"]);
  });

  it("offers no chart at all when the preview came back with no rows to read", () => {
    expect(chartFieldsOf(["day", "orders"], [])).toBeNull();
  });
});

// Every shape of a third, non-numeric column: a low-cardinality label, an
// identifier, and a constant. All three must derive the same two axes and none of them may put a
// third channel into the spec the dialog sends.
const LABELLED: Array<{ shape: string; columns: string[]; rows: PreviewCell[][] }> = [
  {
    shape: "a low-cardinality label",
    columns: ["day", "engine", "n"],
    rows: [
      ["2026-09-01", "tinybird", 3],
      ["2026-09-01", "planetscale", 4],
      ["2026-09-02", "tinybird", 5],
      ["2026-09-02", "planetscale", 6],
    ],
  },
  {
    shape: "a label that names every row",
    columns: ["day", "order_id", "n"],
    rows: [
      ["2026-09-01", "a", 3],
      ["2026-09-02", "b", 4],
      ["2026-09-03", "c", 5],
    ],
  },
  {
    shape: "a label holding one value for every row",
    columns: ["day", "engine", "n"],
    rows: [
      ["2026-09-01", "tinybird", 3],
      ["2026-09-02", "tinybird", 4],
    ],
  },
];

describe("a preview carrying a label column", () => {
  it.each(LABELLED)("derives day against n beside $shape", ({ columns, rows }) => {
    const fields = chartFieldsOf(columns, rows);
    expect(fields?.x).toBe("day");
    expect(fields?.y).toBe("n");
    expect(fields?.xOptions).toEqual(columns);
    expect(fields?.yOptions).toEqual(["n"]);
  });

  it.each(LABELLED)("offers no series split for $shape", ({ columns, rows }) => {
    expect(chartFieldsOf(columns, rows)?.color).toBeNull();
  });

  it.each(LABELLED)("sends two channels and no more for $shape", ({ columns, rows }) => {
    const fields = chartFieldsOf(columns, rows);
    // Built exactly the way the save dialog builds it, from the derived fields
    // rather than from hand-written arguments: that is the path a colour could
    // creep back in through.
    const spec = timeSeriesSpec({ x: fields!.x, y: fields!.y, color: fields!.color }) as {
      encoding: Record<string, unknown>;
    };
    expect(Object.keys(spec.encoding)).toEqual(["x", "y"]);
    expect(spec.encoding).not.toHaveProperty("color");
  });
});

describe("the spec that is sent", () => {
  it("is the one line mark the allowlist admits, with the two channels typed", () => {
    expect(timeSeriesSpec({ x: "day", y: "orders" })).toEqual({
      $schema: "https://vega.github.io/schema/vega-lite/v5.json",
      mark: "line",
      encoding: {
        x: { field: "day", type: "temporal" },
        y: { field: "orders", type: "quantitative" },
      },
    });
  });

  it("names nothing a server allowlist would refuse — no data, no transform, no expression", () => {
    const spec = timeSeriesSpec({ x: "day", y: "n" });
    expect(Object.keys(spec).sort()).toEqual(["$schema", "encoding", "mark"]);
  });

  it("is a spec the object page's own reader will draw", () => {
    // The two halves of the chart story sit in different folders: this writes
    // the declaration, the object page draws it back off a saved object through
    // the renderer's guard. A spec the writer emits and the reader declines is a
    // saved result that quietly loses its chart, so the coupling is asserted
    // rather than assumed.
    const fields = chartFieldsOf(
      ["day", "engine", "n"],
      [
        ["2026-09-01", "tinybird", 3],
        ["2026-09-01", "planetscale", 4],
        ["2026-09-02", "tinybird", 5],
      ],
    );
    const spec = timeSeriesSpec({ x: fields!.x, y: fields!.y, color: fields!.color });
    expect(guardSpec(spec)).toEqual({ ok: true });
    expect(chartCaption(spec)).toBe("n over day");
  });
});
