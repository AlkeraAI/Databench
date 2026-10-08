// The renderer's wall, held to the shared corpus: every spec the profile marks
// unsafe is refused here too, and nothing the profile admits is.

import { describe, expect, it } from "vitest";

import { corpus, type InvalidCase } from "./corpus";
import { guardSpec } from "./guard";

const invalid = corpus<InvalidCase>("profile/invalid");
const admitted = [...corpus("profile/valid"), ...corpus("profile/altair")];

describe("guardSpec", () => {
  it("has a corpus of unsafe specs to refuse", () => {
    expect(invalid.filter((c) => c.value.unsafe).length).toBeGreaterThanOrEqual(10);
  });

  it.each(invalid.filter((c) => c.value.unsafe).map((c) => [c.name, c.value] as const))(
    "refuses the unsafe %s",
    (_name, c) => {
      const verdict = guardSpec(c.spec);
      // The guard and the profile may meet a spec's keys in different orders, so
      // the refusal is the contract, not which offending key it reached first.
      expect(verdict.ok).toBe(false);
    },
  );

  it.each(admitted.map((c) => [c.name, c.value] as const))("admits %s", (_name, spec) => {
    expect(guardSpec(spec)).toEqual({ ok: true });
  });

  it.each([
    ["an unsafe string filter deep in a layer", { layer: [{ mark: "point", transform: [{ filter: "datum.a.constructor" }] }] }, "layer[0].transform[0].filter"],
    ["a calculate that is not a string", { mark: "point", transform: [{ calculate: { expr: "now()" }, as: "b" }] }, "transform[0].calculate"],
    ["an unsafe expression inside not", { mark: "point", transform: [{ filter: { not: "data('x')" } }] }, "transform[0].filter.not"],
    ["an unsafe expression inside or", { mark: "point", transform: [{ filter: { or: [{ field: "a", gt: 1 }, "this"] } }] }, "transform[0].filter.or[1]"],
    ["an unsafe condition test", { mark: "point", encoding: { color: { condition: [{ test: "window.x", value: "red" }], value: "blue" } } }, "encoding.color.condition[0].test"],
    ["a select event stream", { mark: "point", params: [{ name: "p", select: { type: "point", on: "click" } }] }, "params[0].select.on"],
    ["a string clear stream", { mark: "point", params: [{ name: "p", select: { type: "point", clear: "dblclick" } }] }, "params[0].select.clear"],
    ["an identity paint scale", { mark: "point", encoding: { color: { field: "c", type: "nominal", scale: null } } }, "encoding.color.scale"],
    ["a nested cell", { data: { values: [{ a: [1] }] }, mark: "point" }, "data.values[0]"],
    ["a non-row dataset", { datasets: { d: "x" }, mark: "point" }, "datasets.d"],
    ["CSV text", { data: { values: "a,b\n1,2" }, mark: "point" }, "data.values"],
    ["a CSV format over rows", { data: { values: [{ a: 1 }], format: { type: "CSV" } }, mark: "point" }, "data.format"],
    ["a DSV format in a layer", { layer: [{ data: { values: [], format: { type: "dsv", delimiter: "|" } }, mark: "point" }] }, "layer[0].data.format"],
    ["a vega (not lite) schema", { $schema: "https://vega.github.io/schema/vega/v6.json", mark: "point" }, "$schema"],
  ])("refuses %s", (_what, spec, path) => {
    const verdict = guardSpec(spec);
    expect(verdict).toMatchObject({ ok: false, path });
  });

  it.each([
    ["a regression's on field", { mark: "line", transform: [{ regression: "y", on: "x" }] }],
    ["a string filter deep in a layer", { layer: [{ mark: "point", transform: [{ filter: "datum.a > 1" }] }] }],
    ["an expression inside and", { mark: "point", transform: [{ filter: { and: [{ field: "a", gt: 1 }, "datum.b < 3"] } }] }],
    ["text that looks like code outside a predicate", { mark: "text", title: "this.constructor", encoding: {} }],
    ["data text that mentions url(", { data: { values: [{ note: "see url(x)" }] }, mark: "text" }],
    ["a JSON format", { data: { values: [{ a: 1 }], format: { type: "json" } }, mark: "point" }],
    ["a scale-less size channel", { mark: "point", encoding: { size: { field: "s", type: "quantitative", scale: null } } }],
  ])("admits %s", (_what, spec) => {
    expect(guardSpec(spec)).toEqual({ ok: true });
  });

  it("never puts a value into its reason", () => {
    const verdict = guardSpec({ data: { url: "https://evil.example/x" }, mark: "point" });
    expect(verdict.ok).toBe(false);
    if (!verdict.ok) expect(verdict.reason).not.toContain("evil");
  });

  it("refuses a value that is not an object", () => {
    expect(guardSpec("mark: line")).toMatchObject({ ok: false, path: "" });
  });
});
