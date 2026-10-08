// A saved result's chart, as a persisted declaration rather than code.
//
// The spec is the Alkera chart profile under its bound policy: marks, encodings
// and transforms naming the result's column KEYS, never rows of its own. The
// server admits it at write time (`alkera_core.schemas.objects.chart`); the
// renderer (`<AlkeraChart>` from @alkera/ui) refuses anything that could fetch
// or evaluate before Vega sees it, and binds the rows handed to it here.

import type { ChartRow } from "@alkera/ui";

/** One page of a result's rows, as the rows route sends it: the display labels
 *  and the stable column keys behind them, in the same order. */
export interface ChartRows {
  keys: readonly string[];
  rows: readonly unknown[][];
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** The stored chart, or null when the result has none. Whether it is safe to
 *  draw is the renderer's question, answered on its own terms. */
export function chartSpecOf(spec: unknown): Record<string, unknown> | null {
  return isRecord(spec) ? spec : null;
}

function cell(value: unknown): ChartRow[string] {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  return JSON.stringify(value);
}

/** The rows as objects keyed by column KEY, the names a chart's encoding uses.
 *
 *  The binding is by key, never by the label on screen: labels are editable,
 *  so a reader that looked the label up would silently stop drawing the chart
 *  the moment someone renamed a column. */
export function rowsForChart(page: ChartRows): ChartRow[] {
  return page.rows.map((row) => {
    const out: ChartRow = {};
    page.keys.forEach((key, i) => {
      out[key] = cell(row[i]);
    });
    return out;
  });
}

/** What the chart is called when its spec names no title of its own: its
 *  measure over its axis, when it has both. */
export function chartCaption(spec: Record<string, unknown>): string | null {
  if (spec.title !== undefined) return null;
  const encoding = isRecord(spec.encoding) ? spec.encoding : {};
  const field = (channel: string): string | null => {
    const slot = encoding[channel];
    return isRecord(slot) && typeof slot.field === "string" && slot.field ? slot.field : null;
  };
  const x = field("x");
  const y = field("y");
  return x && y ? `${y} over ${x}` : null;
}
