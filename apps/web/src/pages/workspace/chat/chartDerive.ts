// The chart a result gets offered, derived from the preview the agent already
// showed — never from a picker.
//
// The save dialog offers exactly one shape (no chart-type picker, no series
// split, no drill-down), so there is exactly one question worth asking of the
// data: does this preview read as a time series? That is true when one
// column is a date and at least one other is a number. Two date columns is not a
// time series with an obvious axis, so nothing is offered rather than the wrong
// thing guessed; no date column at all means no chart option appears.
//
// The spec this produces is a small corner of the Alkera chart profile the
// server admits (`alkera_core.schemas.objects.chart`): one `line` mark and two
// typed channels, `x` and `y`. The profile and the renderer handle far more
// (series splits, every v1 mark); this dialog simply does not offer them. It
// can carry no `data`, no `transform`, no `url` and no expression, because it
// is built here from field NAMES only — the rows never enter the spec. The
// server still validates it; this side simply cannot construct something the
// server would refuse.

/** The Vega-Lite dialect the server's allowlist admits, spelled once. */
export const VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json";

/** Column names that are a date by their name alone, whatever the values look
 *  like — a `day` column of integers is still the axis. */
const DATE_NAME = /(^|_)(date|day|dt|ts|time|timestamp|month|week|year|hour|minute|bucket)($|_)|_at$/i;

/** A value that reads as a date: an ISO-ish `YYYY-MM-DD`, with or without a
 *  time after it. Deliberately narrow — `Date.parse` accepts "12" and every
 *  browser disagrees about the rest. */
const ISO_DATE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}|$)/;

export type PreviewCell = string | number | boolean | null;

/** What a preview offers to chart: the proposed axes, and what else the reader
 *  may choose instead. */
export interface ChartFields {
  /** The proposed x column — the one that reads as a date. */
  x: string;
  /** Every column, so the reader can move the axis. */
  xOptions: string[];
  /** The proposed y column — the first that reads as a number. */
  y: string;
  /** Only the numeric columns: a measure has to be measurable. */
  yOptions: string[];
  /** Always null. A series split is a second line, which this dialog does not
   *  offer (the profile admits it and the renderer draws it; a spec written
   *  elsewhere may carry one). The slot stays so the dialog that reads a
   *  choice off these fields keeps compiling; it goes when that caller stops
   *  naming it. */
  color: null;
}

/** The two fields a spec is built from, once the reader has chosen. */
export interface ChartChoice {
  x: string;
  y: string;
  /** Accepted and never emitted, for the reason above. */
  color?: null;
}

const present = (cell: PreviewCell): boolean => cell !== null && cell !== undefined && cell !== "";

function columnValues(rows: readonly PreviewCell[][], index: number): PreviewCell[] {
  return rows.map((row) => row[index] ?? null).filter(present);
}

/** Whether a column is the time axis: its name says so, or every value it
 *  actually carries is an ISO date. */
function readsAsDate(name: string, values: readonly PreviewCell[]): boolean {
  if (DATE_NAME.test(name)) return true;
  if (values.length === 0) return false;
  return values.every((value) => typeof value === "string" && ISO_DATE.test(value));
}

/** Whether a column is a measure: it has values, and every one of them is a
 *  finite number. A boolean is not a measurement, and `Number("")` is 0 — which
 *  is why the empties were dropped before this. */
function readsAsNumber(values: readonly PreviewCell[]): boolean {
  if (values.length === 0) return false;
  return values.every(
    (value) => typeof value !== "boolean" && Number.isFinite(Number(value)),
  );
}

/**
 * The chart this preview earns, or `null` when it earns none.
 *
 * Null means the option is not shown at all — a preview with no date column, or
 * with more than one, or with nothing numeric beside it, is not a time series
 * and no amount of picking would make it one.
 */
export function chartFieldsOf(
  columns: readonly string[],
  rows: readonly PreviewCell[][],
): ChartFields | null {
  if (columns.length === 0 || rows.length === 0) return null;
  const values = columns.map((_, index) => columnValues(rows, index));

  const dates = columns.filter((name, index) => readsAsDate(name, values[index] ?? []));
  if (dates.length !== 1) return null;
  const x = dates[0] as string;

  const numeric = columns.filter(
    (name, index) => name !== x && readsAsNumber(values[index] ?? []),
  );
  if (numeric.length === 0) return null;

  // A label column sitting beside the axes earns nothing: the dialog offers no
  // series split, so it is not part of the offer.
  return {
    x,
    xOptions: [...columns],
    y: numeric[0] as string,
    yOptions: numeric,
    color: null,
  };
}

/** The persisted declaration for a choice: the whole of what is sent, and a
 *  strict subset of the chart profile. */
export function timeSeriesSpec(choice: ChartChoice): Record<string, unknown> {
  const encoding: Record<string, unknown> = {
    x: { field: choice.x, type: "temporal" },
    y: { field: choice.y, type: "quantitative" },
  };
  return { $schema: VEGA_LITE_V5_SCHEMA, mark: "line", encoding };
}
