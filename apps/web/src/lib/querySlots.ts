// The one slot grammar a saved query is written in — the browser's half.
//
// A slot has two spellings and both are this grammar:
//
// * `{customer}` (and `{period.start}` / `{period.end}`) — the compiler's own;
// * `{{String(customer)}}` — Tinybird's, with the type in the statement.
//
// The second one is not a nicety. An agent asked for a parameterised statement
// writes the engine's syntax, because that is what the engine binds; reading
// only the first spelling would show such a query as taking no parameters, so
// "change the customer and re-run" would have no field to change.
//
// The server's `slots_of` (`alkera_core/schemas/objects/query_params.py`)
// answers the same for the same template, and one committed fixture drives
// both readers (`src/tests/lib/querySlots.test.ts` and
// `packages/api-core/tests/schemas/objects/test_query_slot_grammar.py`), so a
// spelling one side learns and the other does not is a red test rather than a
// form field that never appears or a value that cannot bind.
//
// Nothing here builds SQL. It reads names and types out of a template; the
// values travel as values and the server's compiler binds them.

/** The parameter types a saved query may declare — the compiler's own
 *  vocabulary (`ParamType` in `alkera_core/schemas/objects/specs.py`, pinned
 *  equal by `querySpecSeam.test.ts`). */
export const PARAM_TYPES = [
  "string",
  "integer",
  "number",
  "boolean",
  "date",
  "datetime",
  "daterange",
  "enum",
] as const;
export type ParamType = (typeof PARAM_TYPES)[number];

/** Tinybird's template type functions and the value kind each one names. These
 *  are the types Tinybird's engine parses a form field back to and refuses when
 *  it does not fit, so a slot written this way carries its own declaration. */
export const TEMPLATE_SLOT_TYPES: Record<string, ParamType> = {
  String: "string",
  Int32: "integer",
  Int64: "integer",
  Float64: "number",
  Date: "date",
  DateTime: "datetime",
  Boolean: "boolean",
};

/** One slot as the template spells it. */
export type QuerySlot = { name: string; type: ParamType };

// Longest type name first so `DateTime` is not read as `Date` followed by junk.
const TYPE_NAMES = Object.keys(TEMPLATE_SLOT_TYPES)
  .sort((a, b) => b.length - a.length)
  .join("|");

/**
 * The grammar, in three alternatives and in this order:
 *
 * 1. a typed slot — `{{String(name)}}`, whitespace tolerated;
 * 2. ANY OTHER doubled-brace group, matched only so it is consumed and
 *    discarded. Without it a bare `{{customer}}` — Tinybird's INTERPOLATION
 *    directive, which writes text into the statement rather than binding
 *    beside it — would be read as our own `{customer}` slot one character in;
 * 3. a bare slot — `{name}`, `{name.start}`, `{name.end}`.
 *
 * Everything else in braces is the author's SQL and is left alone: a JSON
 * literal, a native ClickHouse `{x:Type}` placeholder, an unknown type
 * function, a spaced or numeric name.
 */
const SLOT = new RegExp(
  `\\{\\{\\s*(${TYPE_NAMES})\\s*\\(\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\)\\s*\\}\\}` +
    `|\\{\\{[^{}]*\\}\\}` +
    `|\\{([A-Za-z_][A-Za-z0-9_]*)(?:\\.(start|end))?\\}`,
  "g",
);

/**
 * Every distinct slot in first-appearance order, with the type its spelling
 * declares.
 *
 * A bare `{name}` declares nothing and defaults to `string`; `{name.start}` /
 * `{name.end}` make one `daterange`, and a part outranks a bare use of the
 * same name; a typed `{{Int64(name)}}` declares its type, and the FIRST typed
 * spelling of a name is the one that counts.
 */
export function slotsOf(template: string): QuerySlot[] {
  const found: QuerySlot[] = [];
  const typed = new Set<string>();
  for (const match of template.matchAll(SLOT)) {
    const typeText = match[1] ?? "";
    const name = typeText ? (match[2] ?? "") : (match[3] ?? "");
    if (!name) continue;
    const type: ParamType = typeText
      ? TEMPLATE_SLOT_TYPES[typeText]
      : match[4]
        ? "daterange"
        : "string";
    const existing = found.find((slot) => slot.name === name);
    if (!existing) {
      found.push({ name, type });
      if (typeText) typed.add(name);
      continue;
    }
    if (type === "daterange") {
      existing.type = "daterange";
    } else if (typeText && !typed.has(name) && existing.type !== "daterange") {
      existing.type = type;
      typed.add(name);
    }
  }
  return found;
}

// --------------------------------------------------------------------------
// Turning the filters an agent WROTE AS LITERALS into parameters
// --------------------------------------------------------------------------
//
// Asked a plain question, an agent writes its filter as a literal
// (`WHERE run_day >= toDate('2026-08-31')`) because nothing asked it for a slot.
// Saving that statement gives a query with no parameters, so the reader's only
// way to change the filter is to ask the question again.
//
// So a statement that declares NO slot is offered its own filters as
// parameters. The rule is deliberately narrow — a column compared to a
// literal, and nothing else — and the values that were in the statement become
// the form's defaults, so a re-run that changes nothing runs the query that
// was saved. Nothing is guessed about intent: `brand_kind = 'competitor'`
// becomes a parameter the reader may change and, until they do, still says
// `'competitor'`.

/** The most parameters worth offering: past this the form stops being a form.
 *  A statement with more filters than this keeps its remaining literals. */
export const MAX_DERIVED_PARAMS = 8;

/** Casts whose argument is the value — the slot replaces the whole call, so
 *  `run_day >= toDate('2026-08-31')` becomes `run_day >= {run_day_from}` and
 *  the compiler binds a date. Any other function around a literal is left
 *  alone: replacing it would change what the statement computes. */
const DATE_CASTS = "toDate|toDateTime|toDateOrNull|toDateTimeOrNull|DATE|date";

const FILTER = new RegExp(
  // A boundary, so a column is a whole word rather than the tail of one.
  `(^|[\\s(,])` +
    // The column, optionally qualified: `run_day`, `m.run_day`.
    `([A-Za-z_][A-Za-z0-9_]*(?:\\.[A-Za-z_][A-Za-z0-9_]*)?)` +
    // The comparison.
    `(\\s*(?:<=|>=|<>|!=|=|<|>)\\s*)` +
    // The literal: a cast around a quoted value, a quoted value, or a number.
    `((?:${DATE_CASTS})\\s*\\(\\s*'[^']*'\\s*\\)|'[^']*'|-?\\d+(?:\\.\\d+)?)`,
  "g",
);

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?/;
const QUOTED = /'([^']*)'/;

/** What one comparison's right-hand side offers: the value to bind and the
 *  type to bind it as. */
function valueOf(literal: string): { value: string | number; type: ParamType } | null {
  const quoted = QUOTED.exec(literal);
  if (quoted) {
    const text = quoted[1];
    if (ISO_DATETIME.test(text)) return { value: text, type: "datetime" };
    if (ISO_DATE.test(text)) return { value: text, type: "date" };
    // A cast around something that is not a date is not a date, whatever the
    // cast claims — the value is what binds, so it is typed by what it is.
    return { value: text, type: "string" };
  }
  if (/^-?\d+$/.test(literal)) return { value: Number(literal), type: "integer" };
  if (/^-?\d+\.\d+$/.test(literal)) return { value: Number(literal), type: "number" };
  return null;
}

/** A name for the parameter behind one comparison: the column, and — when the
 *  same column is compared twice — which end of the range it is. */
function nameFor(column: string, operator: string, taken: Set<string>): string | null {
  const base = (column.split(".").pop() ?? column).toLowerCase();
  if (!/^[a-z_][a-z0-9_]*$/.test(base)) return null;
  const op = operator.trim();
  // A range end says which end it is, so a window reads as `run_day_from` /
  // `run_day_to` rather than as one name and a numbered twin.
  const wanted = op === ">=" || op === ">" ? `${base}_from` : op === "<=" || op === "<" ? `${base}_to` : base;
  if (!taken.has(wanted)) return wanted;
  for (let n = 2; n < 20; n += 1) if (!taken.has(`${wanted}_${n}`)) return `${wanted}_${n}`;
  return null;
}

export type DerivedParameters = {
  /** The statement with each offered filter's literal replaced by a slot. */
  template: string;
  params: QuerySlot[];
  /** The literal each slot stands for, so a re-run that changes nothing runs
   *  the statement that was saved. */
  defaults: Record<string, string | number>;
  /** The exact source text each slot replaced — how a caller (or a test)
   *  proves the rewrite is reversible. */
  replaced: { name: string; text: string }[];
};

const NOTHING_DERIVED: DerivedParameters = {
  template: "",
  params: [],
  defaults: {},
  replaced: [],
};

/**
 * The parameters a statement that declares none can be offered, from the
 * literals it compares its columns to.
 *
 * Returns the statement unchanged with no parameters when there is nothing
 * safe to offer — a statement with no filter, one whose quoting this cannot
 * read (an escaped `''`), or one that already declares a slot, which is the
 * author's own answer and is never second-guessed.
 */
export function parametersFromLiterals(sql: string): DerivedParameters {
  if (!sql || slotsOf(sql).length > 0) return { ...NOTHING_DERIVED, template: sql };
  const params: QuerySlot[] = [];
  const defaults: Record<string, string | number> = {};
  const replaced: { name: string; text: string }[] = [];
  const taken = new Set<string>();
  const pieces: string[] = [];
  let cursor = 0;
  for (const match of sql.matchAll(FILTER)) {
    if (params.length >= MAX_DERIVED_PARAMS) break;
    const index = match.index ?? 0;
    const [whole, boundary, column, operator, literal] = match;
    // A quote immediately after the literal means the quoting is doubled
    // (`'it''s'`) and this reader has mis-split it — leave the statement alone.
    if (sql[index + whole.length] === "'") continue;
    const read = valueOf(literal);
    if (!read) continue;
    const name = nameFor(column, operator, taken);
    if (!name) continue;
    taken.add(name);
    params.push({ name, type: read.type });
    defaults[name] = read.value;
    replaced.push({ name, text: literal });
    pieces.push(sql.slice(cursor, index), boundary, column, operator, `{${name}}`);
    cursor = index + whole.length;
  }
  if (params.length === 0) return { ...NOTHING_DERIVED, template: sql };
  pieces.push(sql.slice(cursor));
  return { template: pieces.join(""), params, defaults, replaced };
}
