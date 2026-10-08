import { describe, expect, it } from "vitest";

import { delimitedFormat } from "./vegaData";

describe("delimited data in a chart spec", () => {
  it.each([
    ["CSV at the top", { data: { values: "a,b\n1,2", format: { type: "csv" } } }, "csv"],
    ["TSV in a layer", { layer: [{ mark: "bar" }, { data: { values: "a\tb", format: { type: "TSV" } } }] }, "tsv"],
    ["DSV in a named dataset", { datasets: {}, data: { name: "x", format: { type: "dsv", delimiter: "|" } } }, "dsv"],
    ["a CSV url", { data: { url: "x.csv", format: { type: "csv" } } }, "csv"],
  ])("finds %s", (_name, spec, format) => {
    expect(delimitedFormat(spec)).toBe(format);
  });

  it.each([
    ["JSON records", { data: { values: [{ a: 1 }] }, mark: "bar" }],
    ["a column called format in the rows", { data: { values: [{ format: { type: "csv" } }] } }],
    ["a JSON format", { data: { values: [], format: { type: "json" } } }],
    ["not an object", "csv"],
  ])("lets %s through", (_name, spec) => {
    expect(delimitedFormat(spec)).toBeNull();
  });
});
