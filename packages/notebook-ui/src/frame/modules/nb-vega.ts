// Vega-Lite outputs (v5 and v6). The renderer's guard (`@alkera/chart-guard`,
// the same one every Alkera chart passes) runs first, so an output holds
// to the chart profile's rules: nothing that loads, no `expr` or `signal`, and
// every expression string inside the profile's grammar and allowlist. Then the
// spec is compiled to Vega and run with the expression interpreter over a
// parsed AST, so no expression is ever turned into code at run time (the
// frame's policy refuses `eval` and `Function`). The loader refuses every URL:
// a chart draws from the data it carries, never from the network.

import { guardSpec } from "@alkera/chart-guard";
import { logger, parse, View, Warn, type Loader, type Spec } from "vega";
import { expressionInterpreter } from "vega-interpreter";
import { compile, type TopLevelSpec } from "vega-lite";

import { chartConfig } from "../../outputs/chartTheme";
import type { OutputTheme } from "../../outputs/types";
import type { FrameModule } from "../protocol";
import { DELIMITED_REFUSAL, delimitedFormat } from "../../outputs/vegaData";
import { outputJson, registerFrameModule } from "./register";

export const URL_REFUSED = "This chart names a URL to load, which outputs cannot do.";

/** What the reader is told when the guard refuses a spec: where, and why,
 *  never the value. */
export function guardRefusal(path: string, reason: string): string {
  return `This chart was not drawn: ${path || "the spec"}: ${reason}.`;
}

/** A vega loader that answers every request with a refusal. */
export function refusingLoader(): Loader {
  const refuse = (): Promise<never> => Promise.reject(new Error(URL_REFUSED));
  return { load: refuse, sanitize: refuse, http: refuse, file: refuse };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The config a spec is compiled with: the Alkera chart theme for `theme`
 *  on the frame's own (transparent) ground, under the spec's own config.
 *  Whatever the author set wins, key by key within each section, so a chart
 *  that names its colours keeps them in either theme. */
export function themedConfig(theme: OutputTheme, own: unknown): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...chartConfig(theme), background: "transparent" };
  if (!isRecord(own)) return merged;
  for (const [key, value] of Object.entries(own)) {
    const themed = merged[key];
    merged[key] = isRecord(themed) && isRecord(value) ? { ...themed, ...value } : value;
  }
  return merged;
}

function describe(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

/** Draws a Vega-Lite spec into `container`; resolves once it has rendered.
 *  Vega reports errors, and a failed load (every load fails here), through its
 *  logger rather than by rejecting, so they go to `onError` for the reader. */
export async function renderVegaLite(
  container: HTMLElement,
  spec: Record<string, unknown>,
  theme: OutputTheme,
  onError: (message: string) => void = () => {},
): Promise<View> {
  const compiled = compile({ ...spec, config: themedConfig(theme, spec.config) } as unknown as TopLevelSpec);
  const runtime = parse(compiled.spec as Spec, undefined, { ast: true });
  const log = logger(Warn);
  log.error = (...args: unknown[]) => {
    onError(args.map(describe).join(" "));
    return log;
  };
  // A failed load is only a warning to vega; here it is the refusal the reader
  // needs to see. Other warnings stay vega's own.
  const warn = log.warn.bind(log);
  log.warn = (...args: unknown[]) => {
    if (args[0] === "Loading failed") onError(describe(args[args.length - 1]));
    else warn(...args);
    return log;
  };
  const view = new View(runtime, {
    expr: expressionInterpreter,
    loader: refusingLoader(),
    logger: log,
    renderer: "svg",
    container,
    hover: true,
  });
  await view.runAsync();
  return view;
}

export const vegaModule: FrameModule = {
  mimes: ["application/vnd.vegalite.v5+json", "application/vnd.vegalite.v6+json"],
  async render(init, api) {
    const spec = outputJson(init.data);
    const report = (message: string): void => api.reportError(message);
    if (delimitedFormat(spec) !== null) {
      report(DELIMITED_REFUSAL);
      return;
    }
    const verdict = guardSpec(spec);
    if (!verdict.ok) {
      report(guardRefusal(verdict.path, verdict.reason));
      return;
    }
    let view = await renderVegaLite(api.root, spec, api.theme(), report);
    api.onMessage((message) => {
      if (message.type !== "theme") return;
      view.finalize();
      void renderVegaLite(api.root, spec, message.theme, report).then(
        (next) => {
          view = next;
        },
        (error: unknown) => api.reportError(describe(error)),
      );
    });
  },
};

registerFrameModule("nb-vega", vegaModule);
