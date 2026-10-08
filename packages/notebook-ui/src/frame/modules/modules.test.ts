import { afterEach, describe, expect, it } from "vitest";

import type { FrameApi, FrameInit, FrameModule, ParentMessage } from "../protocol";
import { htmlModule, renderHtml } from "./nb-html";
import { svgModule } from "./nb-svg";
import { themeLayout } from "./nb-plotly";
import { refusingLoader, renderVegaLite, themedConfig, URL_REFUSED, vegaModule } from "./nb-vega";
import { outputJson, outputText } from "./register";

function api(root: HTMLElement, theme: "light" | "dark" = "light"): FrameApi & { listeners: Array<(m: ParentMessage) => void>; errors: unknown[] } {
  const listeners: Array<(m: ParentMessage) => void> = [];
  const errors: unknown[] = [];
  return {
    root,
    listeners,
    errors,
    post: () => {},
    theme: () => theme,
    onMessage: (listener) => listeners.push(listener),
    loadModule: async () => {},
    reportError: (e) => errors.push(e),
  };
}

const init = (mime: string, data: unknown): FrameInit => ({
  type: "init",
  theme: "light",
  output_id: "o",
  mime,
  data,
  opens: [],
  readonly: true,
});

afterEach(() => {
  document.body.replaceChildren();
});

describe("nb-html", () => {
  it("runs the output's inline scripts, in order, like Jupyter", () => {
    const root = document.createElement("div");
    document.body.append(root);
    renderHtml(
      root,
      '<p id="a">one</p><script>document.getElementById("a").textContent += ", two"</script>' +
        '<script>document.getElementById("a").textContent += ", three"</script>',
    );
    expect(root.querySelector("#a")?.textContent).toBe("one, two, three");
  });

  it("accepts the list-of-lines form notebooks store", async () => {
    const root = document.createElement("div");
    await htmlModule.render(init("text/html", ["<b>", "bold", "</b>"]), api(root));
    expect(root.innerHTML).toBe("<b>bold</b>");
  });
});

describe("nb-svg", () => {
  it("draws an SVG output", async () => {
    const root = document.createElement("div");
    await svgModule.render(init("image/svg+xml", '<svg xmlns="http://www.w3.org/2000/svg" width="10"><circle r="4"/></svg>'), api(root));
    expect(root.querySelector("svg circle")).not.toBeNull();
  });

  it("refuses something that is not SVG", () => {
    const root = document.createElement("div");
    expect(() => svgModule.render(init("image/svg+xml", "<html><body>nope</body></html>"), api(root))).toThrow(
      "This SVG output could not be read.",
    );
  });
});

const BARS = {
  $schema: "https://vega.github.io/schema/vega-lite/v6.json",
  data: { values: [{ a: "x", b: 2 }, { a: "y", b: 5 }] },
  mark: "bar",
  encoding: { x: { field: "a", type: "nominal" }, y: { field: "b", type: "quantitative" } },
};

describe("nb-vega", () => {
  it("draws a Vega-Lite chart with the interpreter", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    await vegaModule.render(init("application/vnd.vegalite.v6+json", BARS), api(root));
    expect(root.querySelectorAll("svg path.mark-rect, svg .mark-rect path").length).toBeGreaterThan(0);
  });

  it("evaluates expressions without generating code", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const original = globalThis.Function;
    const calls: unknown[] = [];
    globalThis.Function = new Proxy(original, {
      construct(target, args) {
        calls.push(args);
        return Reflect.construct(target, args);
      },
      apply(target, self, args) {
        calls.push(args);
        return Reflect.apply(target, self, args);
      },
    });
    try {
      await renderVegaLite(
        root,
        { ...BARS, transform: [{ calculate: "datum.b * 2 + (datum.a === 'x' ? 1 : 0)", as: "c" }, { filter: "datum.c > 3" }] },
        "light",
      );
    } finally {
      globalThis.Function = original;
    }
    expect(calls).toEqual([]);
  });

  it("draws an Altair spec whose filter, calculate and condition are expressions", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const frameApi = api(root);
    const spec = {
      ...BARS,
      transform: [{ calculate: "datum.b * 2", as: "c" }, { filter: "(datum.c > 5)" }],
      encoding: {
        ...BARS.encoding,
        color: { condition: { test: "(datum.b > 4)", value: "crimson" }, value: "gray" },
      },
    };
    await vegaModule.render(init("application/vnd.vegalite.v5+json", spec), frameApi);
    expect(frameApi.errors).toEqual([]);
    expect(root.querySelectorAll("svg .mark-rect path")).toHaveLength(1);
  });

  it.each([
    ["an expression reaching a prototype", { transform: [{ calculate: "datum.a.constructor", as: "c" }] }, "transform[0].calculate"],
    ["a function outside the allowlist", { transform: [{ filter: "data('elsewhere')" }] }, "transform[0].filter"],
    ["a signal read in a condition", { encoding: { ...BARS.encoding, color: { condition: { test: "width > 0", value: "red" }, value: "blue" } } }, "encoding.color.condition.test"],
    ["an expr value", { mark: { type: "bar", size: { expr: "width" } } }, "mark.size.expr"],
    ["data from a URL", { data: { url: "https://example.com/data.json" } }, "data.url"],
  ])("refuses %s before Vega runs, naming the path", async (_what, change, path) => {
    const root = document.createElement("div");
    document.body.append(root);
    const frameApi = api(root);
    await vegaModule.render(init("application/vnd.vegalite.v5+json", { ...BARS, ...change }), frameApi);
    expect(frameApi.errors).toHaveLength(1);
    expect(String(frameApi.errors[0])).toContain(`${path}:`);
    expect(String(frameApi.errors[0])).not.toMatch(/elsewhere|constructor|example\.com/);
    expect(root.querySelector("svg")).toBeNull();
  });

  it("refuses to load data from a URL", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const errors: string[] = [];
    const spec = { ...BARS, data: { url: "https://example.com/data.json" } };
    await renderVegaLite(root, spec, "light", (message) => errors.push(message));
    expect(errors.join(" ")).toContain(URL_REFUSED);
    expect(root.querySelectorAll(".mark-rect path")).toHaveLength(0);
  });

  it.each(["load", "sanitize", "http", "file"] as const)("the loader refuses %s", async (method) => {
    const loader = refusingLoader() as unknown as Record<string, (...args: unknown[]) => Promise<unknown>>;
    await expect(loader[method]("https://example.com/x", {})).rejects.toThrow(URL_REFUSED);
  });

  it("redraws in the new theme", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const frameApi = api(root);
    await vegaModule.render(init("application/vnd.vegalite.v6+json", BARS), frameApi);
    const before = root.innerHTML;
    for (const listener of frameApi.listeners) listener({ type: "theme", theme: "dark" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(root.innerHTML).not.toBe(before);
    // The Alkera chart theme's dark label ink and first dark series colour.
    expect(before).not.toContain("#a39d92");
    expect(root.innerHTML).toContain("#a39d92");
    expect(before).toContain("#2a78d6");
    expect(root.innerHTML).toContain("#3987e5");
  });

  it("keeps the colours a chart's author named, in either theme", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const spec = { ...BARS, mark: { type: "bar", color: "#ff00aa" }, config: { axis: { labelColor: "#123456" }, background: "#fedcba" } };
    for (const theme of ["light", "dark"] as const) {
      await renderVegaLite(root, spec, theme);
      expect(root.innerHTML).toContain("#ff00aa");
      expect(root.innerHTML).toContain("#123456");
      expect(root.innerHTML).toMatch(/#fedcba|rgb\(254, 220, 186\)/);
    }
  });
});

describe("the config a framed chart is compiled with", () => {
  it("is the Alkera chart theme on a transparent ground", () => {
    const dark = themedConfig("dark", undefined);
    expect(dark.background).toBe("transparent");
    expect(dark.axis).toMatchObject({ labelColor: "#a39d92", titleColor: "#ece9e2", gridColor: "rgba(231, 226, 216, 0.11)" });
    expect((dark.range as { category: string[] }).category[0]).toBe("#3987e5");
    const light = themedConfig("light", null);
    expect(light.axis).toMatchObject({ labelColor: "#5f5b53", titleColor: "#1b1a17" });
    expect((light.range as { category: string[] }).category[0]).toBe("#2a78d6");
  });

  it("lets the spec's own config win key by key, keeping the rest of a section themed", () => {
    const merged = themedConfig("dark", { axis: { labelColor: "#123456" }, background: "#000000", range: { category: ["#111111"] }, numberFormat: ".2f" });
    expect(merged.axis).toMatchObject({ labelColor: "#123456", titleColor: "#ece9e2" });
    expect(merged.background).toBe("#000000");
    expect(merged.range).toMatchObject({ category: ["#111111"], ramp: expect.arrayContaining(["#3987e5"]) });
    expect(merged.numberFormat).toBe(".2f");
  });
});

describe("the layout a framed Plotly figure is themed with", () => {
  it("is the Alkera chart theme's ink, grid and series on a clear ground", () => {
    expect(themeLayout("dark")).toEqual({
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      "font.color": "#ece9e2",
      colorway: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
      "xaxis.gridcolor": "rgba(231, 226, 216, 0.11)",
      "xaxis.linecolor": "rgba(231, 226, 216, 0.2)",
      "xaxis.zerolinecolor": "rgba(231, 226, 216, 0.2)",
      "yaxis.gridcolor": "rgba(231, 226, 216, 0.11)",
      "yaxis.linecolor": "rgba(231, 226, 216, 0.2)",
      "yaxis.zerolinecolor": "rgba(231, 226, 216, 0.2)",
    });
    expect(themeLayout("light")["font.color"]).toBe("#1b1a17");
  });

  it.each([
    ["a top-level colour", { paper_bgcolor: "#ffffff" }, ["paper_bgcolor"]],
    ["a nested colour", { font: { color: "#ff0000" }, xaxis: { gridcolor: "#00ff00" } }, ["font.color", "xaxis.gridcolor"]],
    ["a dotted key", { "yaxis.linecolor": "#0000ff" }, ["yaxis.linecolor"]],
    ["its own series colours", { colorway: ["#111111"] }, ["colorway"]],
  ])("leaves out what the figure's layout sets: %s", (_name, layout, kept) => {
    const themed = themeLayout("dark", layout);
    for (const key of kept) expect(themed).not.toHaveProperty([key]);
    expect(Object.keys(themed)).toHaveLength(10 - kept.length);
  });

  it("themes what only a template sets", () => {
    const themed = themeLayout("dark", { template: { layout: { paper_bgcolor: "white", font: { color: "#2a3f5f" } } }, xaxis: { title: "x" } });
    expect(themed.paper_bgcolor).toBe("rgba(0,0,0,0)");
    expect(themed["font.color"]).toBe("#ece9e2");
    expect(themed["xaxis.gridcolor"]).toBe("rgba(231, 226, 216, 0.11)");
  });
});

describe("output data", () => {
  it("joins lines and parses JSON text", () => {
    expect(outputText(["a", "b"])).toBe("ab");
    expect(outputText(3)).toBe("");
    expect(outputJson('{"a":1}')).toEqual({ a: 1 });
    expect(() => outputJson("[1]")).toThrow("This output is not a chart specification.");
  });
});

describe("registration", () => {
  it("each module registers itself with the bootstrap when its code runs", async () => {
    const registered: Array<[string, FrameModule]> = [];
    window.__alkRegister = (name, _version, module) => registered.push([name, module]);
    try {
      const { registerFrameModule } = await import("./register");
      registerFrameModule("nb-html", htmlModule);
      expect(registered.map(([name]) => name)).toEqual(["nb-html"]);
    } finally {
      delete window.__alkRegister;
    }
  });
});
