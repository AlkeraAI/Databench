// From a stored spec plus the host's tables to the exact Vega-Lite the runtime
// compiles. Pure data in, data out: no Vega import, so it is unit-testable and
// costs the initial bundle nothing.

/** One row of a table, keyed by column key. */
export type ChartRow = Record<string, string | number | boolean | null>;
export type ChartRows = readonly ChartRow[];

export interface ChartTables {
  /** The table a bound spec (one with no data of its own, like a saved
   *  result's chart) draws: the result's rows. */
  data?: ChartRows;
  /** Named tables the host supplies for `data: {name}` references. */
  datasets?: Readonly<Record<string, ChartRows>>;
}

type Spec = Record<string, unknown>;

const COMPOSITIONS = ["layer", "hconcat", "vconcat", "concat", "facet"] as const;

const isRecord = (v: unknown): v is Spec => typeof v === "object" && v !== null && !Array.isArray(v);

/** Whether any view in the spec brings its own data. */
function hasOwnData(spec: Spec): boolean {
  if ("data" in spec) return true;
  for (const key of COMPOSITIONS) {
    const children = spec[key];
    if (Array.isArray(children) && children.some((c) => isRecord(c) && hasOwnData(c))) return true;
  }
  return isRecord(spec.spec) && hasOwnData(spec.spec);
}

/** A single view (a mark or a layer) can stretch to its container; a
 *  composition keeps its natural size and scrolls. */
export function isSingleView(spec: Spec): boolean {
  return "mark" in spec || "layer" in spec;
}

export class MissingTableError extends Error {}

/**
 * The spec with the host's tables bound into it.
 *
 * A spec with no data reads the bound table as inline values. A named reference
 * the spec's own `datasets` does not carry is filled from the host's tables;
 * one neither provides is a `MissingTableError`, since Vega-Lite would
 * otherwise draw an empty chart without a word.
 */
export function bindTables(spec: Spec, tables: ChartTables): Spec {
  const out: Spec = { ...spec };
  if (!hasOwnData(spec)) {
    out.data = { values: tables.data ? [...tables.data] : [] };
  }
  const carried = isRecord(spec.datasets) ? spec.datasets : {};
  const needed = new Set<string>();
  const collect = (node: unknown): void => {
    if (Array.isArray(node)) node.forEach(collect);
    else if (isRecord(node)) {
      const data = node.data;
      if (isRecord(data) && typeof data.name === "string" && !("values" in data)) needed.add(data.name);
      for (const key of [...COMPOSITIONS, "spec"]) collect(node[key]);
    }
  };
  collect(spec);
  const merged: Record<string, unknown> = { ...carried };
  for (const name of needed) {
    if (name in carried) continue;
    const host = tables.datasets?.[name];
    if (!host) throw new MissingTableError(`no table is bound to a named data reference`);
    merged[name] = [...host];
  }
  if (Object.keys(merged).length > 0) out.datasets = merged;
  return out;
}

/** Whether a single view leaves its width to its container. */
export function fillsWidth(spec: Spec): boolean {
  return isSingleView(spec) && (spec.width === undefined || spec.width === "container");
}

/** A width to fill when the container reports none (a hidden or unmeasured
 *  element). */
export const FALLBACK_WIDTH = 480;

/**
 * The spec sized for its container: a single view without a numeric width
 * fills `width` (the container's, measured by the host, which also resizes it
 * later); one without a height takes `height`. Measured here rather than with
 * Vega-Lite's `"container"`, whose size follows only window resizes and reads
 * zero in an unlaid-out element.
 */
export function sizeFor(spec: Spec, width: number | undefined, height: number | undefined): Spec {
  if (!isSingleView(spec)) return spec;
  const out: Spec = { ...spec };
  if (fillsWidth(spec)) {
    out.width = width && width > 0 ? width : FALLBACK_WIDTH;
    if (out.autosize === undefined) out.autosize = { type: "fit-x", contains: "padding" };
  }
  if (out.height === undefined && height !== undefined) out.height = height;
  return out;
}
