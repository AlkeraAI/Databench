// Every expression the chart profile admits (the shared corpus,
// `packages/api-core/tests/fixtures/charts/profile/expressions`) is one Vega's
// own parser reads and the renderer draws through the interpreter: the guard
// and the profile admit nothing the runtime then fails on.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { parseExpression } from "vega";
import { beforeAll, describe, expect, it } from "vitest";

import { CORPUS } from "./corpus";
import { DEFAULT_CHART_TOKENS } from "./theme";
import { renderToSvg } from "./vegaRuntime";

interface Admit {
  name: string;
  expression: string;
  fields: string[];
}

const admit = JSON.parse(readFileSync(join(CORPUS, "profile/expressions/admit.json"), "utf8")) as Admit[];

describe("an admitted expression is one Vega reads and draws", () => {
  // jsdom has no canvas; Vega then estimates text widths, which is all a test needs.
  beforeAll(() => {
    HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
  });

  it("has the corpus", () => {
    expect(admit.length).toBeGreaterThanOrEqual(30);
  });

  it.each(admit.map((c) => [c.name, c] as const))("Vega parses %s", (_name, c) => {
    expect(() => parseExpression(c.expression)).not.toThrow();
  });

  it.each(admit.map((c) => [c.name, c] as const))("draws %s through the interpreter", async (_name, c) => {
    // A string that reads as a number suits both the string and the math functions.
    const row = Object.fromEntries(c.fields.map((f) => [f, "2"]));
    const spec = {
      data: { values: [row] },
      transform: [{ calculate: c.expression, as: "out" }],
      mark: "point",
      encoding: { x: { field: "out", type: "nominal" } },
    };
    const svg = await renderToSvg(spec, { tokens: DEFAULT_CHART_TOKENS.light, width: 200, height: 100 });
    expect(svg).toContain("mark-symbol");
  });
});
