// Every chart the profile admits is drawn by the renderer, and nothing it draws
// reaches for eval or the network.

import { parse, View } from "vega";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { corpus, type InvalidCase } from "./corpus";
import { DEFAULT_CHART_TOKENS } from "./theme";
import { ChartRefusedError, compileChart, lockedLoader, mountChart, renderToSvg } from "./vegaRuntime";

const tokens = DEFAULT_CHART_TOKENS.light;

// jsdom has no canvas; Vega then estimates text widths, which is all a test needs.
beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
});
const drawable = [...corpus("profile/valid"), ...corpus("profile/altair")];
const interactive = drawable.filter(({ value }) => JSON.stringify(value).includes('"params"'));

/** The SVG elements Vega draws data marks as, by mark role. */
const MARK_ROLE = /class="mark-(symbol|line|area|rect|rule|text|arc|trail)[^"]*role-mark/g;

describe("renderToSvg over the corpus", () => {
  it("has the corpus to draw", () => {
    expect(drawable.length).toBeGreaterThanOrEqual(40);
    expect(interactive.length).toBeGreaterThanOrEqual(6);
  });

  it.each(drawable.map((c) => [c.name, c.value] as const))("draws %s with data marks", async (_name, spec) => {
    const svg = await renderToSvg(spec, { tokens, width: 400, height: 200 });
    expect(svg.startsWith("<svg")).toBe(true);
    const marks = svg.match(MARK_ROLE) ?? [];
    expect(marks.length).toBeGreaterThan(0);
    // A mark group with items has at least one drawn path, shape or text.
    expect(svg).toMatch(/<(path|line|text)\b[^>]*(d="M|x1=|transform=)/);
  });
});

describe("no eval", () => {
  const realFunction = globalThis.Function;
  const realEval = globalThis.eval;

  /** Run `body` with code generation forbidden; report whether it was tried. */
  async function forbidCodegen(body: () => Promise<void>): Promise<boolean> {
    let attempted = false;
    const trap = function trap(): never {
      attempted = true;
      throw new EvalError("code generation is forbidden");
    };
    globalThis.Function = trap as unknown as FunctionConstructor;
    globalThis.eval = trap as unknown as typeof eval;
    try {
      await body();
    } catch {
      // A thrown trap is the evidence; it is reported through `attempted`.
    } finally {
      globalThis.Function = realFunction;
      globalThis.eval = realEval;
    }
    return attempted;
  }

  it("catches stock Vega, which compiles expressions with Function()", async () => {
    const vega = compileChart(interactive[0]!.value, { tokens, width: 300, height: 150 });
    const attempted = await forbidCodegen(async () => {
      await new View(parse(vega as never)).runAsync();
    });
    expect(attempted).toBe(true);
  });

  it.each(interactive.map((c) => [c.name, c.value] as const))(
    "draws interactive %s with Function and eval forbidden",
    async (_name, spec) => {
      const host = document.createElement("div");
      document.body.append(host);
      let chart: Awaited<ReturnType<typeof mountChart>> | undefined;
      const attempted = await forbidCodegen(async () => {
        chart = await mountChart(host, spec, { tokens, width: 400, height: 200 });
      });
      expect(attempted).toBe(false);
      expect(chart).toBeDefined();
      expect(host.querySelector("svg")).not.toBeNull();
      chart?.finalize();
      host.remove();
    },
  );
});

describe("no network", () => {
  it("never requests anything while drawing the whole corpus", async () => {
    const fetchSpy = vi.fn(() => Promise.reject(new Error("no network")));
    const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, "open");
    const imageSrc = vi.fn();
    const descriptor = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
    Object.defineProperty(HTMLImageElement.prototype, "src", { configurable: true, set: imageSrc, get: () => "" });
    vi.stubGlobal("fetch", fetchSpy);
    try {
      for (const { value } of drawable) {
        const host = document.createElement("div");
        document.body.append(host);
        (await mountChart(host, value, { tokens, width: 320, height: 160 })).finalize();
        host.remove();
      }
    } finally {
      vi.unstubAllGlobals();
      xhrOpen.mockRestore();
      if (descriptor) Object.defineProperty(HTMLImageElement.prototype, "src", descriptor);
    }
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpen).not.toHaveBeenCalled();
    expect(imageSrc).not.toHaveBeenCalled();
  });

  it.each([
    ["load", () => lockedLoader.load("https://example.test/rows.json")],
    ["sanitize", () => lockedLoader.sanitize("https://example.test/a.png", { context: "image" } as never)],
    ["http", () => lockedLoader.http("https://example.test/", {})],
    ["file", () => lockedLoader.file("/etc/passwd")],
  ])("the loader refuses %s", async (_what, call) => {
    await expect(call()).rejects.toThrow(/charts load nothing/);
  });
});

describe("the guard runs before Vega", () => {
  const unsafe = corpus<InvalidCase>("profile/invalid").filter((c) => c.value.unsafe);

  it.each(unsafe.map((c) => [c.name, c.value] as const))("refuses %s without rendering", async (_name, c) => {
    await expect(renderToSvg(c.spec, { tokens })).rejects.toBeInstanceOf(ChartRefusedError);
  });
});

describe("mountChart", () => {
  it("fills a single view to the container and follows a resize", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const spec = corpus("profile/valid").find((c) => c.name === "line_multi_series")!.value;
    const chart = await mountChart(host, spec, { tokens, width: 300, height: 120 });
    const widthOf = () => Number(host.querySelector("svg")!.getAttribute("width"));
    expect(chart.fills).toBe(true);
    expect(widthOf()).toBe(300);
    await chart.resize(520);
    expect(widthOf()).toBe(520);
    chart.finalize();
    host.remove();
  });

  it("removes its tooltip when finalized", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const spec = corpus("profile/valid").find((c) => c.name === "circle")!.value;
    const before = document.querySelectorAll(".alk-chart-tooltip").length;
    const chart = await mountChart(host, spec, { tokens });
    expect(document.querySelectorAll(".alk-chart-tooltip")).toHaveLength(before + 1);
    chart.finalize();
    expect(document.querySelectorAll(".alk-chart-tooltip")).toHaveLength(before);
    host.remove();
  });
});
