// The output renderers: the registry, the components that draw outputs, and
// each app renderer with its helpers.

export type * from "./types";
export {
  MIME_PREFERENCE,
  OutputRegistry,
  defaultOutputRegistry,
  orderedMimes,
  pickRenderer,
  registerOutputRenderer,
  unregisterOutputRenderer,
  type RendererChoice,
} from "./registry";
export {
  BundleView,
  MAX_BUNDLE_DEPTH,
  OutputArea,
  OutputView,
  mergeStreams,
  outputContext,
  type BundleViewProps,
  type OutputAreaProps,
  type OutputViewProps,
} from "./OutputView";
export { appRenderers, registerDefaultOutputRenderers } from "./defaults";
export { PLAIN_STYLE, applySgr, collapseCarriageReturns, parseAnsi, stripAnsi, xtermColor } from "./ansi";
export type { AnsiColor, AnsiSegment, AnsiStyle } from "./ansi";
export { AnsiText, toText } from "./AnsiText";
export { ErrorOutput, PlainText, StreamOutput, textRenderer } from "./text";
export { IMAGE_MIMES, downloadImage, imageBase64, imageDataUrl, imageFile, imageRenderer, type ImageMime } from "./image";
export { extractMath, renderMarkdown, renderMath, type MathSpan } from "./markdown";
export { MarkdownView, markdownRenderer } from "./MarkdownOutput";
export { JSON_CHILD_LIMIT, JsonTree, jsonRenderer } from "./JsonTree";
export { FILTER_DEBOUNCE_MS, TableView, readTablePayload, rowRecords, tableRenderer, type TableSort, type TableViewProps } from "./TableView";
export {
  buildFilterSql,
  cellText,
  containsPattern,
  filterRowsLocally,
  quoteIdent,
  quoteLiteral,
  sortRowsLocally,
  tableToCsv,
} from "./tableQuery";
export { layoutRenderer, parseLayout, type CalloutKind, type LayoutAlign, type LayoutPayload } from "./LayoutOutput";
export {
  ChartFallback,
  VegaChartEngine,
  chartRenderer,
  chartRows,
  currentChartEngine,
  drawVegaChart,
  refusingLoader,
  registerChartEngine,
  type ChartEngine,
  type ChartEngineProps,
  type ChartSpec,
} from "./chart";
export { CHART_TOKENS, chartConfig } from "./chartTheme";
export { DELIMITED_REFUSAL, delimitedFormat } from "./vegaData";
