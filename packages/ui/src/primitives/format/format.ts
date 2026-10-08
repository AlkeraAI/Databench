// How the product renders a machine's facts to a person: a count, an instant, a
// span, and the parameters a statement was bound with.
//
// These four live here, in one module, because the same four facts appear on
// two surfaces at once — a tool card in the transcript and the receipt panel on
// the object that card was saved into — and a reader comparing them is doing
// exactly what a receipt is for, so both read values through these functions
// and never show two clocks or two formats on the one screen.
//
// A value the machine did not send is not formatted into a zero. Callers decide
// what absence looks like (the receipt panel shows a dash); these functions are
// given a number, a timestamp or a payload, and return the reading of it.

/** A whole number with its thousands separated: `1234567` → `1,234,567`.
 *
 *  Grouped one way everywhere on purpose. The digits are the same fact on the
 *  card and on the receipt, so they are grouped by the product rather than by
 *  whatever locale the reader's machine happens to carry — two people comparing
 *  the same result over a call must be reading the same string. */
export function formatCount(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString("en-US") : String(value);
}

/** An instant, in the reader's own locale AND their own zone, named.
 *
 *  The zone is part of the fact: a result stamped at noon UTC read at 5am in
 *  California is not a result from this morning unless the reading says which
 *  clock it is on. A value that is not a time is returned as it came — an
 *  unparseable stamp is still evidence, and hiding it behind a dash would lose
 *  the only copy. */
export function formatTimestamp(value: string | number | Date): string {
  const at = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(at.getTime())) return String(value);
  // Spelled out component by component rather than as `dateStyle`/`timeStyle`:
  // ECMA-402 refuses those beside `timeZoneName`, and the zone is the half that
  // makes the reading answerable. The components are the medium styles' own.
  return at.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });
}

/** A span in the unit a reader thinks in: `412` → `412 ms`, `1234` → `1.2 s`,
 *  `95_000` → `1 min 35 s`. Sub-second work is counted in milliseconds because
 *  that is the difference between a warm cache and a cold one; past a minute
 *  nobody reads `95000`. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return String(ms);
  const span = Math.abs(ms);
  if (span < 1000) return `${Math.round(ms)} ms`;
  if (span < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(span / 60_000);
  const seconds = Math.round((span % 60_000) / 1000);
  return `${ms < 0 ? "-" : ""}${minutes} min ${seconds} s`;
}

/** One bound parameter, as a person reads it. */
export interface FormattedParam {
  /** The name the statement binds — `customer` in `WHERE customer = {customer}`. */
  name: string;
  /** Its value, rendered: a string as itself, anything else as its JSON. */
  value: string;
}

/**
 * The parameters a statement was bound with, as a label/value list.
 *
 * A receipt's `params` is an object, and `JSON.stringify` on the trust surface
 * asks the reader to parse braces to answer "which customer was this?". A list
 * of pairs is the same fact, read at a glance. A payload that is not an object
 * of parameters (an array, a bare value, nothing) yields no pairs, and the
 * caller renders the absence in its own vocabulary.
 */
export function formatParams(value: unknown): FormattedParam[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([name, bound]) => ({
    name,
    value: formatParamValue(bound),
  }));
}

function formatParamValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number") return formatCount(value);
  if (typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
