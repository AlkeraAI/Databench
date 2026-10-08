// The renderer's own wall: a spec that could fetch, or evaluate anything but a
// bounded expression over its rows, is refused before Vega sees it.
//
// The Alkera chart profile (`alkera_core.charts.profile`) is the authority on
// what a chart may say, and every writer validates against it. This guard is
// not a second copy of it. It is the reader's check on data it did not write,
// narrowed to the one question that matters in a browser: could rendering this
// spec reach the network or run code? Expressions (a `calculate`, a string
// `filter`, a condition's string `test`) are admitted only inside the profile's
// grammar and allowlist (`./expression`). A stored spec from an older or newer
// writer, a spec pasted into a notebook, a spec a compromised writer stored:
// each meets this before it meets Vega. Behind it the runtime still runs Vega
// with the expression interpreter (no `eval`) and a loader that refuses every
// load, so a gap here is caught again there.
//
// Refusals name the path and the reason, never a value: the value is the
// customer's data, or the injected string.

import { checkExpression } from "./expression";

const SCHEMA = /^https:\/\/vega\.github\.io\/schema\/vega-lite\/v[56](\.\d{1,3}){0,2}\.json$/;

/** Keys that are a fetch, a link, or code in every place Vega-Lite reads them. */
const FORBIDDEN_KEYS: ReadonlyMap<string, string> = new Map([
  ["url", "it loads a resource"],
  ["href", "it opens a link"],
  ["expr", "it is an expression"],
  ["signal", "it is an expression"],
  ["signals", "it declares expressions"],
  ["labelExpr", "it is an expression"],
  ["update", "it is an expression"],
  ["init", "it is an expression"],
]);

/** Keys whose value is a predicate: a structured object, or an expression
 *  string. Inside one, `and`, `or` and `not` hold predicates too. */
const PREDICATE_KEYS = new Set(["filter", "test"]);
const LOGICAL_KEYS = new Set(["and", "or", "not"]);

/** Delimited-text formats: Vega parses them by compiling a row function. */
const DELIMITED_FORMATS = new Set(["csv", "tsv", "dsv"]);

/** The only bindings that are interactions inside the chart. */
const ALLOWED_BINDS = new Set(["scales", "legend"]);

/** Channels whose `scale: null` would paint a data value verbatim. */
const PAINT_CHANNELS = new Set(["color", "fill", "stroke"]);

const MAX_DEPTH = 48;

export type GuardResult = { ok: true } | { ok: false; path: string; reason: string };

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const join = (path: string, key: string): string => (path ? `${path}.${key}` : key);

class Refusal extends Error {
  constructor(
    readonly path: string,
    readonly reason: string,
  ) {
    super(`${path || "the chart"}: ${reason}`);
  }
}

/** Data rows are the customer's values. They are drawn as text and positions,
 *  never interpreted, so the walk does not read into them; it only checks that
 *  they are rows. */
function checkRows(rows: unknown, path: string): void {
  if (!Array.isArray(rows)) throw new Refusal(path, "data values must be a list of rows");
  rows.forEach((row, i) => {
    if (!isRecord(row)) throw new Refusal(`${path}[${i}]`, "a row must be an object");
    for (const cell of Object.values(row)) {
      if (cell !== null && typeof cell === "object") {
        throw new Refusal(`${path}[${i}]`, "a cell must be a string, number, boolean or null");
      }
    }
  });
}

function expression(text: unknown, path: string): void {
  const verdict = checkExpression(text);
  if (!verdict.ok) throw new Refusal(path, `the expression ${verdict.reason}`);
}

/** `predicate` is set while walking a predicate: a string there is an
 *  expression, not text. */
function walk(node: unknown, path: string, depth: number, parentKey: string, predicate = false): void {
  if (depth > MAX_DEPTH) throw new Refusal(path, "the chart nests too deeply");
  if (typeof node === "string") {
    if (predicate) expression(node, path);
    else if (/url\s*\(/i.test(node)) throw new Refusal(path, "a style may not reference a resource");
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((item, i) => walk(item, `${path}[${i}]`, depth + 1, parentKey, predicate));
    return;
  }
  if (!isRecord(node)) return;
  for (const [key, value] of Object.entries(node)) {
    const at = join(path, key);
    const forbidden = FORBIDDEN_KEYS.get(key);
    if (forbidden) throw new Refusal(at, `${key} is not allowed: ${forbidden}`);
    if ((key === "on" || (key === "clear" && typeof value === "string")) && parentKey === "select") {
      throw new Refusal(at, "a selection may not declare its own event handlers");
    }
    if (key === "calculate") {
      expression(value, at);
      continue;
    }
    if (key === "bind" && !(typeof value === "string" && ALLOWED_BINDS.has(value))) {
      throw new Refusal(at, "a selection may bind only to the chart's scales or legend");
    }
    if (key === "mark" && (value === "image" || (isRecord(value) && value.type === "image"))) {
      throw new Refusal(at, "an image mark loads a resource");
    }
    if (key === "scale" && value === null && PAINT_CHANNELS.has(parentKey)) {
      throw new Refusal(at, "a color must come through a scale");
    }
    if (
      key === "format" &&
      parentKey === "data" &&
      isRecord(value) &&
      typeof value.type === "string" &&
      DELIMITED_FORMATS.has(value.type.toLowerCase())
    ) {
      throw new Refusal(at, "CSV and TSV data are not parsed; rows must be inline objects");
    }
    if (key === "values" && parentKey === "data") {
      checkRows(value, at);
      continue;
    }
    if (key === "datasets" && path === "") {
      if (!isRecord(value)) throw new Refusal(at, "datasets must be an object of row lists");
      for (const [name, rows] of Object.entries(value)) checkRows(rows, join(at, name));
      continue;
    }
    if (key === "$schema" && path === "") {
      if (typeof value !== "string" || !SCHEMA.test(value)) {
        throw new Refusal(at, "only a Vega-Lite v5 or v6 schema is allowed");
      }
      continue;
    }
    walk(value, at, depth + 1, key, PREDICATE_KEYS.has(key) || (predicate && LOGICAL_KEYS.has(key)));
  }
}

/** Whether `spec` is safe to hand to the renderer. */
export function guardSpec(spec: unknown): GuardResult {
  if (!isRecord(spec)) return { ok: false, path: "", reason: "a chart must be an object" };
  try {
    walk(spec, "", 0, "");
    return { ok: true };
  } catch (error) {
    if (error instanceof Refusal) return { ok: false, path: error.path, reason: error.reason };
    throw error;
  }
}
