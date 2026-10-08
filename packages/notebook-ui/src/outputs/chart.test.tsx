import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";

import { CHART_TOKENS } from "./chartTheme";
import {
  VegaChartEngine,
  chartRows,
  currentChartEngine,
  drawVegaChart,
  refusingLoader,
  registerChartEngine,
  type ChartEngineProps,
} from "./chart";
import { registerDefaultOutputRenderers } from "./defaults";
import { OutputView } from "./OutputView";
import { OutputRegistry } from "./registry";
import type { OutputAreaContext } from "./types";

const CHART = "application/vnd.alkera.chart+json";

const barSpec = {
  $schema: "https://vega.github.io/schema/vega-lite/v6.json",
  mark: "bar",
  data: {
    values: [
      { region: "north", sales: 3 },
      { region: "south", sales: 5 },
      { region: "east", sales: 2 },
    ],
  },
  encoding: {
    x: { field: "region", type: "nominal" },
    y: { field: "sales", type: "quantitative" },
  },
};

// Vega measures text on a canvas when it can; jsdom has none and says so on
// every call. Vega falls back to estimating, which is what the test wants.
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(() => {
  vi.restoreAllMocks();
});

function drawOutput(spec: unknown, theme: "light" | "dark" = "light") {
  const registry = new OutputRegistry();
  registerDefaultOutputRenderers(registry);
  const context: OutputAreaContext = { theme, readonly: false, cellId: "c" };
  return render(<OutputView output={{ output_id: "o", type: "display", data: { [CHART]: spec, "text/plain": "chart" } }} context={context} registry={registry} />);
}

describe("the chart engine seam", () => {
  it("draws with the registered engine and puts the previous one back", () => {
    const Fake = ({ spec, theme }: ChartEngineProps) => <p>{`fake ${String(spec.mark)} ${theme}`}</p>;
    const Other = () => <p>other engine</p>;
    const restoreFake = registerChartEngine(Fake);
    try {
      drawOutput(barSpec, "dark");
      expect(screen.getByText("fake bar dark")).toBeInTheDocument();

      // Swapping while mounted redraws with the new engine.
      let restoreOther = () => {};
      act(() => {
        restoreOther = registerChartEngine(Other);
      });
      expect(screen.getByText("other engine")).toBeInTheDocument();
      act(() => restoreOther());
      expect(screen.getByText("fake bar dark")).toBeInTheDocument();
    } finally {
      restoreFake();
    }
    expect(currentChartEngine()).toBe(VegaChartEngine);
  });

  it("a stale restore does not remove a newer engine", () => {
    const A = () => null;
    const B = () => null;
    const restoreA = registerChartEngine(A);
    const restoreB = registerChartEngine(B);
    restoreA();
    expect(currentChartEngine()).toBe(B);
    restoreB();
    expect(currentChartEngine()).toBe(A);
    restoreA();
    expect(currentChartEngine()).toBe(VegaChartEngine);
  });

  it("refuses a value that is not a spec object", () => {
    drawOutput(["not", "a", "spec"]);
    expect(screen.getByText("This chart could not be read.")).toBeInTheDocument();
  });
});

describe("the default engine's loader", () => {
  it.each([
    ["load", () => refusingLoader.load()],
    ["sanitize", () => refusingLoader.sanitize()],
    ["http", () => refusingLoader.http()],
    ["file", () => refusingLoader.file()],
  ])("refuses %s", async (_name, call) => {
    await expect(call()).rejects.toThrow("Charts load nothing from outside the notebook");
  });

  it("never fetches a data URL in a spec", async () => {
    const fetches: unknown[] = [];
    vi.stubGlobal("fetch", (...args: unknown[]) => {
      fetches.push(args);
      return Promise.reject(new Error("network"));
    });
    const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, "open");
    try {
      const element = document.createElement("div");
      const spec = { ...barSpec, data: { url: "https://evil.example/rows.json" } };
      await drawVegaChart(element, spec, "light").then(
        (finalize) => finalize(),
        () => undefined,
      );
      expect(fetches).toEqual([]);
      expect(xhrOpen).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("the default engine", () => {
  it("draws a bar chart as SVG in the theme's colors", async () => {
    const element = document.createElement("div");
    const finalize = await drawVegaChart(element, barSpec, "light");
    const bars = element.querySelectorAll(".mark-rect path");
    expect(bars).toHaveLength(3);
    expect(bars[0].getAttribute("fill")).toBe(CHART_TOKENS.light.category[0]);
    finalize();

    const dark = document.createElement("div");
    const finalizeDark = await drawVegaChart(dark, barSpec, "dark");
    expect(dark.querySelector(".mark-rect path")?.getAttribute("fill")).toBe(CHART_TOKENS.dark.category[0]);
    expect(dark.querySelector("svg")?.getAttribute("style")).toContain("rgb(37, 35, 30)");
    finalizeDark();
  });

  it("draws through the output renderer", async () => {
    const { container } = drawOutput(barSpec);
    await waitFor(() => expect(container.querySelectorAll(".nb-output-chart .mark-rect path")).toHaveLength(3));
  });

  it("falls back to the spec's rows as a table with a one-line reason", async () => {
    drawOutput({ ...barSpec, mark: "not-a-mark" });
    expect(await screen.findByText(/^Chart could not be drawn: /)).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("south")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /sales/ })).toBeInTheDocument();
  });

  it("says a refused load is why a chart with a data URL is empty", async () => {
    drawOutput({ ...barSpec, data: { url: "https://evil.example/rows.json" } });
    expect(await screen.findByText(/^Chart could not be drawn: /)).toHaveTextContent("Charts load nothing from outside the notebook");
  });
});

describe("chartRows", () => {
  it.each([
    ["inline values", { data: { values: [{ a: 1 }] } }, [{ a: 1 }]],
    ["the named dataset", { data: { name: "b" }, datasets: { a: [{ x: 1 }], b: [{ y: 2 }] } }, [{ y: 2 }]],
    ["the first dataset", { datasets: { a: [{ x: 1 }] } }, [{ x: 1 }]],
    ["nothing usable", { data: { url: "x" } }, []],
    ["non-row values", { data: { values: [1, 2] } }, []],
  ])("reads %s", (_name, spec, rows) => {
    expect(chartRows(spec)).toEqual(rows);
  });
});

describe("delimited data", () => {
  it("is refused with an output error before Vega runs", async () => {
    const context = { theme: "light" as const, readonly: true, cellId: "c", outputId: "o", renderBundle: () => null };
    render(<VegaChartEngine spec={{ mark: "bar", data: { values: "a,b\n1,2", format: { type: "csv" } } } as never} theme="light" context={context} />);
    expect(await screen.findByText(/CSV or TSV text/)).toBeInTheDocument();
  });
});
