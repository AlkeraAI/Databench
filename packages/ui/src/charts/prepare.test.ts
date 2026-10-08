import { describe, expect, it } from "vitest";

import { bindTables, FALLBACK_WIDTH, MissingTableError, sizeFor } from "./prepare";

const ROWS = [{ a: 1 }, { a: 2 }];

describe("bindTables", () => {
  it("gives a spec with no data the bound table", () => {
    expect(bindTables({ mark: "point" }, { data: ROWS })).toEqual({ mark: "point", data: { values: ROWS } });
  });

  it("leaves a spec that carries rows (at any depth) alone", () => {
    const spec = { layer: [{ data: { values: [{ b: 1 }] }, mark: "point" }] };
    expect(bindTables(spec, { data: ROWS })).toEqual(spec);
  });

  it("fills a named reference from the host, keeping the spec's own datasets first", () => {
    const spec = {
      datasets: { own: [{ c: 1 }] },
      vconcat: [{ data: { name: "own" }, mark: "point" }, { data: { name: "host" }, mark: "bar" }],
    };
    const bound = bindTables(spec, { datasets: { host: ROWS, own: [{ c: 9 }] } });
    expect(bound.datasets).toEqual({ own: [{ c: 1 }], host: ROWS });
  });

  it("refuses a named reference nobody provides", () => {
    expect(() => bindTables({ data: { name: "gone" }, mark: "point" }, {})).toThrow(MissingTableError);
  });
});

describe("sizeFor", () => {
  it.each([
    ["no width", {}, 360, 360],
    ["container", { width: "container" }, 360, 360],
    ["an unmeasured container", {}, 0, FALLBACK_WIDTH],
  ])("fills the container for %s", (_what, extra, measured, expected) => {
    const sized = sizeFor({ mark: "line", ...extra }, measured, 200);
    expect(sized).toMatchObject({ width: expected, height: 200, autosize: { type: "fit-x" } });
  });

  it.each([
    ["a numeric width", { mark: "line", width: 120 }],
    ["a step width", { mark: "bar", width: { step: 12 } }],
    ["a composition", { hconcat: [{ mark: "line" }] }],
  ])("keeps %s as written", (_what, spec) => {
    expect(sizeFor(spec, 360, undefined)).toEqual(spec);
  });

  it("keeps an authored height", () => {
    expect(sizeFor({ mark: "line", height: 90 }, 300, 200).height).toBe(90);
  });
});
