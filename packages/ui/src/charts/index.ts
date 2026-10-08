// Alkera charts: the renderer for the Alkera chart profile (a validated subset
// of Vega-Lite v6). Everything here is Vega-free; Vega loads on first draw.
export { AlkeraChart, loadChartRuntime, type AlkeraChartProps } from "./AlkeraChart";
export { guardSpec, type GuardResult } from "@alkera/chart-guard";
export { bindTables, type ChartRow, type ChartRows, type ChartTables } from "./prepare";
export {
  chartConfig,
  DEFAULT_CHART_TOKENS,
  readChartTokens,
  schemeOf,
  type ChartConfig,
  type ChartScheme,
  type ChartTokens,
} from "./theme";
