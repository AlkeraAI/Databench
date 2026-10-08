// A saved result's chart on the object page: the stored spec, the rows bound to
// it by column KEY, and the caption it earns. Whether a spec is safe to draw is
// the renderer's guard's question (pinned in @alkera/ui and by the object page
// test); this module only shapes what the page hands it.

import { describe, expect, it } from "vitest";

import { chartCaption, chartSpecOf, rowsForChart } from "@/pages/workspace/objects/chartSpec";

describe("chartSpecOf", () => {
  it.each([
    ["missing", undefined],
    ["null", null],
    ["a string", "mark: line"],
    ["a list", [{ mark: "line" }]],
  ])("is null for a chart that is %s", (_what, spec) => {
    expect(chartSpecOf(spec)).toBeNull();
  });

  it("passes an object through untouched", () => {
    const spec = { mark: "bar", encoding: {} };
    expect(chartSpecOf(spec)).toBe(spec);
  });
});

describe("rowsForChart", () => {
  it("keys each row by column KEY, never by the label on screen", () => {
    const page = { columns: ["Day", "Orders (net)"], keys: ["day", "orders"], rows: [["2026-09-05", 12]] };
    expect(rowsForChart(page)).toEqual([{ day: "2026-09-05", orders: 12 }]);
  });

  it("keeps scalars, turns a missing cell into null and a nested value into its JSON text", () => {
    const page = { keys: ["a", "b", "c", "d", "e"], rows: [[1, "x", true, undefined, { n: 1 }]] };
    expect(rowsForChart(page)).toEqual([{ a: 1, b: "x", c: true, d: null, e: '{"n":1}' }]);
  });

  it("gives a short row's trailing keys null rather than dropping them", () => {
    expect(rowsForChart({ keys: ["a", "b"], rows: [[1]] })).toEqual([{ a: 1, b: null }]);
  });
});

describe("chartCaption", () => {
  const encoding = { x: { field: "day" }, y: { field: "orders" } };

  it("names the measure over the axis", () => {
    expect(chartCaption({ mark: "line", encoding })).toBe("orders over day");
  });

  it.each([
    ["a title of its own (the chart draws it)", { mark: "line", title: "Orders", encoding }],
    ["no y", { mark: "bar", encoding: { x: { field: "day" } } }],
    ["no encoding", { layer: [] }],
    ["a constant y", { mark: "rule", encoding: { x: { field: "day" }, y: { datum: 3 } } }],
  ])("has none for a spec with %s", (_what, spec) => {
    expect(chartCaption(spec)).toBeNull();
  });
});
