// The renderer as one self-contained script, for a page that has no bundler and
// no network: a content-origin frame (`script-src 'unsafe-inline'`, no eval, no
// `connect-src`) inlines it and calls `AlkeraCharts.render`. Built by
// `pnpm --filter @alkera/ui build:charts-frame` into `dist/alkera-charts.iife.js`.

import type { ChartTables } from "./prepare";
import { readChartTokens, type ChartTokens } from "./theme";
import { ChartRefusedError, mountChart, type MountedChart } from "./vegaRuntime";

export interface FrameRenderOptions extends ChartTables {
  /** The parent's tokens, handed over by message; read from the frame's own
   *  stylesheet when absent. */
  tokens?: ChartTokens;
  renderer?: "svg" | "canvas";
  height?: number;
}

export { ChartRefusedError };

/** Draw `spec` into `el`; resolves to the mounted chart (call `finalize`). */
export async function render(el: HTMLElement, spec: unknown, options: FrameRenderOptions = {}): Promise<MountedChart> {
  const chart = await mountChart(el, spec, {
    tokens: options.tokens ?? readChartTokens(el),
    tables: { data: options.data, datasets: options.datasets },
    renderer: options.renderer,
    width: el.clientWidth,
    height: options.height ?? 240,
  });
  if (chart.fills && typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => void chart.resize(el.clientWidth)).observe(el);
  }
  return chart;
}
