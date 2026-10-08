// `application/vnd.alkera.chart+json`: an Alkera chart, a validated subset of
// Vega-Lite v6 whose value is the spec itself.
//
// Drawing goes through a seam: `registerChartEngine` swaps the component
// that draws a spec. The default engine runs Vega in the app, self-contained:
// the expression interpreter instead of compiled code, and a loader that
// refuses every URL, so a chart can never fetch. When it cannot draw, it
// shows the spec's rows as a table with a one-line reason.

import { useEffect, useRef, useState, useSyncExternalStore, type ComponentType } from "react";
import type { Config as VegaLiteConfig, TopLevelSpec } from "vega-lite";

import { chartConfig } from "./chartTheme";
import { TableView } from "./TableView";
import { DELIMITED_REFUSAL, delimitedFormat } from "./vegaData";
import type { OutputContext, OutputRenderer, OutputRendererProps, OutputTheme } from "./types";

export type ChartSpec = Record<string, unknown>;

export interface ChartEngineProps {
  spec: ChartSpec;
  theme: OutputTheme;
  context: OutputContext;
}

export type ChartEngine = ComponentType<ChartEngineProps>;

// -- the engine seam ----------------------------------------------------------

let engine: ChartEngine | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Makes `component` the chart engine. Returns a function that puts back the
 *  engine it replaced (only while it is still the current one). */
export function registerChartEngine(component: ChartEngine): () => void {
  const previous = engine;
  engine = component;
  listeners.forEach((listener) => listener());
  return () => {
    if (engine !== component) return;
    engine = previous;
    listeners.forEach((listener) => listener());
  };
}

/** The engine that draws charts now. */
export function currentChartEngine(): ChartEngine {
  return engine ?? VegaChartEngine;
}

// -- the default engine -------------------------------------------------------

const REFUSAL = "Charts load nothing from outside the notebook";

/** A Vega loader that refuses every load, sanitize, HTTP and file request, so
 *  neither a data URL nor an image mark nor a link reaches the network. */
export const refusingLoader = {
  load: (): Promise<string> => Promise.reject(new Error(REFUSAL)),
  sanitize: (): Promise<{ href: string }> => Promise.reject(new Error(REFUSAL)),
  http: (): Promise<string> => Promise.reject(new Error(REFUSAL)),
  file: (): Promise<string> => Promise.reject(new Error(REFUSAL)),
};

type VegaModules = [typeof import("vega"), typeof import("vega-lite"), typeof import("vega-interpreter")];
let modules: Promise<VegaModules> | null = null;

function loadVega(): Promise<VegaModules> {
  modules ??= Promise.all([import("vega"), import("vega-lite"), import("vega-interpreter")]);
  modules.catch(() => {
    modules = null;
  });
  return modules;
}

/** Compiles and runs `spec` into `container` as SVG. Resolves with a
 *  function that tears the view down. */
export async function drawVegaChart(container: HTMLElement, spec: ChartSpec, theme: OutputTheme): Promise<() => void> {
  const [vega, vegaLite, interpreter] = await loadVega();
  const compiled = vegaLite.compile(spec as unknown as TopLevelSpec, { config: chartConfig(theme) as VegaLiteConfig });
  const runtime = vega.parse(compiled.spec, undefined, { ast: true });
  // Vega logs a failed load (the loader's refusal) and draws on without the
  // data; collect what it logs so the chart says why instead.
  const logged: unknown[] = [];
  const logger = vega.logger(vega.Warn);
  logger.error = (...args: readonly unknown[]) => {
    logged.push(args[0]);
    return logger;
  };
  logger.warn = (...args: readonly unknown[]) => {
    // Vega reports a failed load as a warning: ("Loading failed", url, error).
    if (args[0] === "Loading failed") logged.push(args[2] ?? args[0]);
    return logger;
  };
  const view = new vega.View(runtime, {
    expr: interpreter.expressionInterpreter,
    loader: refusingLoader,
    renderer: "svg",
    hover: true,
    logger,
  });
  try {
    view.initialize(container);
    await view.runAsync();
    if (logged.length > 0) throw logged[0] instanceof Error ? logged[0] : new Error(String(logged[0]));
  } catch (error) {
    view.finalize();
    throw error;
  }
  return () => view.finalize();
}

/** The rows a spec carries inline: `data.values`, the dataset `data.name`
 *  names, or else the first dataset. */
export function chartRows(spec: ChartSpec): Record<string, unknown>[] {
  const isRows = (value: unknown): value is Record<string, unknown>[] =>
    Array.isArray(value) && value.every((row) => typeof row === "object" && row !== null && !Array.isArray(row));
  const data = spec.data as { values?: unknown; name?: unknown } | undefined;
  if (data && isRows(data.values)) return data.values;
  const datasets = spec.datasets as Record<string, unknown> | undefined;
  if (datasets && typeof datasets === "object") {
    if (data && typeof data.name === "string" && isRows(datasets[data.name])) return datasets[data.name] as Record<string, unknown>[];
    const first = Object.values(datasets).find(isRows);
    if (first) return first;
  }
  return [];
}

export function ChartFallback({ spec, reason }: { spec: ChartSpec; reason: string }) {
  const rows = chartRows(spec);
  const names = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  return (
    <div className="nb-output-chart-fallback">
      <p className="nb-output-note nb-output-note--error" role="alert">
        Chart could not be drawn: {reason}
      </p>
      {rows.length > 0 && (
        <TableView
          payload={{
            schema: { fields: names.map((name) => ({ name, type: "" })) },
            data: rows,
            total_rows: rows.length,
          }}
        />
      )}
    </div>
  );
}

export function VegaChartEngine({ spec, theme }: ChartEngineProps) {
  const container = useRef<HTMLDivElement>(null);
  const [failure, setFailure] = useState<string | null>(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    let cancelled = false;
    let teardown: (() => void) | null = null;
    if (delimitedFormat(spec) !== null) {
      setFailure(DELIMITED_REFUSAL);
      return;
    }
    drawVegaChart(element, spec, theme).then(
      (finalize) => {
        if (cancelled) finalize();
        else teardown = finalize;
      },
      (error: unknown) => {
        if (!cancelled) setFailure(oneLine(error));
      },
    );
    return () => {
      cancelled = true;
      teardown?.();
      element.replaceChildren();
    };
  }, [spec, theme]);
  if (failure) return <ChartFallback spec={spec} reason={failure} />;
  return <div className="nb-output-chart" ref={container} />;
}

function oneLine(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  const first = message.split("\n")[0].trim();
  return first.length > 200 ? `${first.slice(0, 199)}…` : first || "unknown error";
}

// -- the renderer -------------------------------------------------------------

function ChartOutput({ data, context }: OutputRendererProps) {
  const Engine = useSyncExternalStore(subscribe, currentChartEngine, currentChartEngine);
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return <p className="nb-output-note">This chart could not be read.</p>;
  }
  return <Engine spec={data as ChartSpec} theme={context.theme} context={context} />;
}

export const chartRenderer: OutputRenderer = {
  id: "alkera.chart",
  mimes: ["application/vnd.alkera.chart+json"],
  rank: 0,
  place: "app",
  Component: ChartOutput,
};
