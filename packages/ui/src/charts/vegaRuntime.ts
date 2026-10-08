// The one module that imports Vega. Everything that draws a chart comes through
// here, so three properties hold by construction rather than by each caller
// remembering them:
//
//   * No eval. Vega compiles expressions with `Function()` unless it is handed
//     an interpreter; every view here is parsed with `{ast: true}` and run with
//     `vega-interpreter`, which passes Alkera's CSP (no `unsafe-eval` anywhere).
//   * No network. The loader refuses every load and every link, so a spec that
//     slipped past the guard and the profile still cannot fetch.
//   * The guard runs first. A spec the guard refuses never reaches Vega-Lite.
//
// It is loaded with a dynamic import (see `AlkeraChart`), so Vega lands in its
// own chunk and never in what a page loads up front. ESLint forbids importing
// vega, vega-lite or vega-embed anywhere else.

import { parse, View, type Loader } from "vega";
import { expressionInterpreter } from "vega-interpreter";
import { compile, type TopLevelSpec } from "vega-lite";

import { guardSpec } from "@alkera/chart-guard";

import { bindTables, fillsWidth, sizeFor, type ChartTables } from "./prepare";
import { chartConfig, type ChartTokens } from "./theme";
import { createTooltip } from "./tooltip";

export class ChartRefusedError extends Error {
  constructor(
    readonly path: string,
    readonly reason: string,
  ) {
    super(`chart refused at ${path || "the spec"}: ${reason}`);
  }
}

export class ChartCompileError extends Error {}

const refuse = (what: string): Promise<never> =>
  Promise.reject(new Error(`charts load nothing: a ${what} was refused`));

/** A loader that loads nothing. Vega asks it for data URLs, images and link
 *  targets; every answer is a refusal. */
export const lockedLoader: Loader = {
  load: () => refuse("data load"),
  sanitize: () => refuse("resource reference"),
  http: () => refuse("network request"),
  file: () => refuse("file read"),
};

export interface CompileOptions {
  tokens: ChartTokens;
  tables?: ChartTables;
  /** The width a single view fills (its container's). */
  width?: number;
  height?: number;
}

/** A stored spec, guarded, bound to its tables, sized and compiled to Vega. */
export function compileChart(spec: unknown, options: CompileOptions): Record<string, unknown> {
  const verdict = guardSpec(spec);
  if (!verdict.ok) throw new ChartRefusedError(verdict.path, verdict.reason);
  const bound = bindTables(spec as Record<string, unknown>, options.tables ?? {});
  const sized = sizeFor(bound, options.width, options.height);
  const warnings: string[] = [];
  const quiet = {
    level: () => quiet,
    error: (...args: readonly unknown[]) => {
      warnings.push(args.map(String).join(" "));
      return quiet;
    },
    warn: () => quiet,
    info: () => quiet,
    debug: () => quiet,
  };
  try {
    return compile(sized as unknown as TopLevelSpec, {
      config: chartConfig(options.tokens),
      logger: quiet as never,
    }).spec as unknown as Record<string, unknown>;
  } catch (error) {
    throw new ChartCompileError(error instanceof Error ? error.message : "the chart could not be compiled");
  }
}

function viewOf(vega: Record<string, unknown>, options: Partial<ConstructorParameters<typeof View>[1]>): View {
  const runtime = parse(vega as never, undefined, { ast: true });
  return new View(runtime, {
    ...options,
    expr: expressionInterpreter,
    loader: lockedLoader,
    logLevel: 0,
  });
}

export interface MountOptions extends CompileOptions {
  renderer?: "svg" | "canvas";
}

export interface MountedChart {
  /** Fit a single view to a new container width. */
  resize: (width: number) => Promise<void>;
  /** Tear the view down: listeners, timers, the tooltip. */
  finalize: () => void;
  /** Whether this chart stretches with its container. */
  readonly fills: boolean;
}

/** Draw `spec` into `container`. */
export async function mountChart(
  container: HTMLElement,
  spec: unknown,
  options: MountOptions,
): Promise<MountedChart> {
  const vega = compileChart(spec, options);
  const tooltip = createTooltip(container.ownerDocument.body, options.tokens);
  const view = viewOf(vega, {
    renderer: options.renderer ?? "svg",
    container,
    hover: true,
    tooltip: tooltip.show,
  });
  try {
    await view.runAsync();
  } catch (error) {
    view.finalize();
    tooltip.destroy();
    throw new ChartCompileError(error instanceof Error ? error.message : "the chart could not be drawn");
  }
  const fills = fillsWidth(spec as Record<string, unknown>);
  return {
    fills,
    resize: async (width) => {
      if (!fills || width <= 0) return;
      await view.width(width).runAsync();
    },
    finalize: () => {
      view.finalize();
      tooltip.destroy();
    },
  };
}

/** Render `spec` headlessly to an SVG string (no DOM container). */
export async function renderToSvg(spec: unknown, options: CompileOptions): Promise<string> {
  const view = viewOf(compileChart(spec, options), { renderer: "none" });
  try {
    return await view.toSVG();
  } finally {
    view.finalize();
  }
}
