// The rules every notebook op applier follows, for the browser's editor.
//
// The owner is `alkera_notebook.format.op_rules` (with the SQL and Markdown
// templates of `alkera_notebook.format.templates`): which text an `edit`
// replaces, what a cell's text becomes when its kind changes, which kinds can
// be inserted, and which text a SQL or Markdown cell can hold. This is that
// module in the browser's language, held to it by the shared op vectors
// (`packages/alkera-notebook/tests/vectors/notebook_ops.json`), which the file
// store, the platform's live document and this editor all run. A rule changes
// there first; a case goes in the vector file, never in one runner.

export type CellMeta = Record<string, unknown>;

/** The kinds an insert or a kind change may name. `unparsable` is what a
 *  reader makes of code it cannot parse, never something an editor creates. */
export const INSERTABLE_KINDS: ReadonlySet<string> = new Set(["setup", "python", "function", "class", "sql", "markdown"]);
/** Kinds whose text is not the code they run as, but rendered through a template. */
export const TEMPLATED_KINDS: ReadonlySet<string> = new Set(["sql", "markdown"]);
/** What no templated kind's text may contain: it would end the template's string. */
export const TRIPLE_QUOTE = '"""';
/** The meta a cell changed to a templated kind starts with. */
export const DEFAULT_META: Readonly<Record<string, CellMeta>> = {
  sql: { output_var: "_df" },
  markdown: { quote: "r" },
};

export type OpRuleCode = "edit_not_found" | "edit_ambiguous" | "unknown_kind" | "not_representable";

/** An op the rules refuse; `code` is the op error code every applier reports. */
export class OpRuleRefused extends Error {
  constructor(
    readonly code: OpRuleCode,
    message: string,
  ) {
    super(message);
  }
}

/** Where an exact-text edit replacing `old` applies in `text`. Matches are
 *  counted without overlap, left to right; `occurrence` (from 1) picks one,
 *  and without it the text must occur once. An empty `old` names only the
 *  whole text of an empty cell. */
export function locateEdit(text: string, old: string, occurrence: number | null): number {
  if (old === "") {
    if (text !== "") throw new OpRuleRefused("edit_ambiguous", "an empty match is ambiguous in a cell with text");
    return 0;
  }
  const found: number[] = [];
  for (let at = text.indexOf(old); at >= 0; at = text.indexOf(old, at + old.length)) found.push(at);
  if (occurrence !== null) {
    const at = occurrence >= 1 ? found[occurrence - 1] : undefined;
    if (at === undefined) throw new OpRuleRefused("edit_not_found", `occurrence ${occurrence} of the text is not there`);
    return at;
  }
  if (found.length === 0) throw new OpRuleRefused("edit_not_found", "the text to replace is not in the cell");
  if (found.length > 1) throw new OpRuleRefused("edit_ambiguous", `the text to replace is there ${found.length} times`);
  return found[0] as number;
}

/** `text` after one exact-text edit, and where the edit applied. */
export function applyEdit(text: string, old: string, replacement: string, occurrence: number | null): { at: number; text: string } {
  const at = locateEdit(text, old, occurrence);
  return { at, text: text.slice(0, at) + replacement + text.slice(at + old.length) };
}

export function checkInsertable(kind: string): void {
  if (!INSERTABLE_KINDS.has(kind)) throw new OpRuleRefused("unknown_kind", `no cell kind ${kind} can be created`);
}

/** The code a cell of `kind` holding `source` is written as. A SQL or Markdown
 *  cell is written through its template, which must read back as the same
 *  kind and text; text it cannot hold is refused. */
export function templatedCode(kind: string, source: string, meta: CellMeta): string {
  if (!TEMPLATED_KINDS.has(kind)) return source;
  if (source.includes(TRIPLE_QUOTE)) {
    throw new OpRuleRefused("not_representable", `a ${kind} cell cannot hold three double quotes in a row; use a Python cell`);
  }
  const code = renderCell(kind, source, meta);
  const got = classify(code);
  if (got.kind !== kind || got.source !== source) {
    throw new OpRuleRefused("not_representable", `this ${kind} cell's settings cannot be written`);
  }
  return code;
}

/** The text and meta a cell holds after its kind changes: into a templated
 *  kind, code that already is the template gives its text and meta, anything
 *  else keeps its text with the kind's default meta (refused when that kind
 *  cannot hold it); out of one, the text becomes the code the cell ran as. */
export function kindChange(fromKind: string, toKind: string, source: string, meta: CellMeta): { source: string; meta: CellMeta } {
  checkInsertable(toKind);
  const code = TEMPLATED_KINDS.has(fromKind) ? renderCell(fromKind, source, meta) : source;
  if (TEMPLATED_KINDS.has(toKind)) {
    const got = classify(code);
    if (got.kind === toKind) return { source: got.source, meta: got.meta };
    const fresh = { ...DEFAULT_META[toKind] };
    templatedCode(toKind, source, fresh);
    return { source, meta: fresh };
  }
  return { source: code, meta: {} };
}

// -- the SQL and Markdown templates -----------------------------------------------

const INDENT = "    ";
const SQL_CALL = "alkera.sql";
const MD_CALL = "alkera.md";
const MARKDOWN_QUOTES = new Set(["r", "rf"]);
const IDENTIFIER = /^[\p{XID_Start}_][\p{XID_Continue}]*$/u;
const KEYWORDS = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif",
  "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or",
  "pass", "raise", "return", "try", "while", "with", "yield",
]);
const SQL_RE = /^(?<var>[\p{XID_Start}_][\p{XID_Continue}]*) = alkera\.sql\(\n {4}rf"""\n(?<body>(?:[^\n]*\n)*?) {4}""",\n(?<options>(?: {4}[^\n]*\n)*)\)$/u;
const MD_RE = /^alkera\.md\(\n {4}(?<quote>rf?)"""\n(?<body>(?:[^\n]*\n)*?) {4}"""\n\)$/u;

function isIdentifier(name: string): boolean {
  return IDENTIFIER.test(name) && !KEYWORDS.has(name);
}

/** Python's `repr` of a string, double-quoted whenever it allows. */
export function pythonString(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let inner = "";
  for (const ch of value) {
    const code = ch.codePointAt(0) as number;
    if (ch === "\\") inner += "\\\\";
    else if (ch === quote) inner += `\\${quote}`;
    else if (ch === "\n") inner += "\\n";
    else if (ch === "\r") inner += "\\r";
    else if (ch === "\t") inner += "\\t";
    else if (code < 0x20 || code === 0x7f) inner += `\\x${code.toString(16).padStart(2, "0")}`;
    else inner += ch;
  }
  if (quote === "'" && !value.includes('"')) return `"${inner}"`;
  return `${quote}${inner}${quote}`;
}

/** The string a quoted Python literal spells, for the spellings
 *  {@link pythonString} writes (anything else fails the round trip after). */
function stringLiteral(text: string): string | null {
  const quote = text[0];
  if ((quote !== '"' && quote !== "'") || text.length < 2 || text[text.length - 1] !== quote) return null;
  let out = "";
  for (let i = 1; i < text.length - 1; i += 1) {
    const ch = text[i] as string;
    if (ch === quote) return null;
    if (ch !== "\\") {
      out += ch;
      continue;
    }
    const next = text[i + 1];
    i += 1;
    if (next === "\\" || next === "'" || next === '"') out += next;
    else if (next === "n") out += "\n";
    else if (next === "r") out += "\r";
    else if (next === "t") out += "\t";
    else if (next === "x" && /^[0-9a-f]{2}$/.test(text.slice(i + 1, i + 3))) {
      out += String.fromCharCode(parseInt(text.slice(i + 1, i + 3), 16));
      i += 2;
    } else return null;
  }
  return out;
}

function indentLines(text: string): string {
  return text
    .split("\n")
    .map((line) => (line ? INDENT + line : "") + "\n")
    .join("");
}

function dedentBody(body: string): string | null {
  const lines = body.split("\n").slice(0, -1);
  const out: string[] = [];
  for (const line of lines) {
    if (line === "") out.push("");
    else if (line.startsWith(INDENT)) out.push(line.slice(INDENT.length));
    else return null;
  }
  return out.join("\n");
}

function renderSql(source: string, meta: CellMeta): string | null {
  const name = meta.output_var ?? "_df";
  const connection = meta.connection ?? null;
  const showOutput = meta.show_output ?? true;
  if (source.includes(TRIPLE_QUOTE) || typeof name !== "string" || !isIdentifier(name)) return null;
  if ((meta.engine ?? null) !== null) return null;
  if (connection !== null && typeof connection !== "string") return null;
  let code = `${name} = ${SQL_CALL}(\n${INDENT}rf"""\n${indentLines(source)}${INDENT}""",\n`;
  if (connection !== null) code += `${INDENT}connection=${pythonString(connection)},\n`;
  if (showOutput === false) code += `${INDENT}output=False,\n`;
  return `${code})`;
}

function renderMarkdown(source: string, meta: CellMeta): string | null {
  const quote = meta.quote ?? "r";
  if (source.includes(TRIPLE_QUOTE) || typeof quote !== "string" || !MARKDOWN_QUOTES.has(quote)) return null;
  return `${MD_CALL}(\n${INDENT}${quote}"""\n${indentLines(source)}${INDENT}"""\n)`;
}

/** The cell code for editor text `source` of `kind`: SQL and Markdown through
 *  their template (their text unchanged when it cannot be), others as is. */
export function renderCell(kind: string, source: string, meta: CellMeta): string {
  const rendered = kind === "sql" ? renderSql(source, meta) : kind === "markdown" ? renderMarkdown(source, meta) : null;
  return rendered ?? source;
}

function classifySql(code: string): { source: string; meta: CellMeta } | null {
  const match = SQL_RE.exec(code);
  const groups = match?.groups;
  if (groups === undefined) return null;
  const source = dedentBody(groups.body ?? "");
  if (source === null) return null;
  let connection: string | null = null;
  let showOutput = true;
  for (const line of (groups.options ?? "").split("\n").slice(0, -1)) {
    const rest = line.slice(INDENT.length);
    const eq = rest.indexOf("=");
    if (eq < 0 || !rest.endsWith(",")) return null;
    const name = rest.slice(0, eq);
    const value = rest.slice(eq + 1);
    if (name === "connection" && connection === null) {
      connection = stringLiteral(value.slice(0, -1));
      if (connection === null) return null;
    } else if (name === "output" && value === "False,") showOutput = false;
    else return null;
  }
  const meta: CellMeta = { output_var: groups.var, connection, engine: null, show_output: showOutput };
  return renderSql(source, meta) === code ? { source, meta } : null;
}

function classifyMarkdown(code: string): { source: string; meta: CellMeta } | null {
  const groups = MD_RE.exec(code)?.groups;
  if (groups === undefined) return null;
  const source = dedentBody(groups.body ?? "");
  if (source === null) return null;
  const meta: CellMeta = { quote: groups.quote };
  return renderMarkdown(source, meta) === code ? { source, meta } : null;
}

/** `(kind, source, meta)` for a cell's code: `sql` or `markdown` when the
 *  code is exactly a template rendering, `python` with the code otherwise. */
export function classify(code: string): { kind: string; source: string; meta: CellMeta } {
  const sql = classifySql(code);
  if (sql !== null) return { kind: "sql", ...sql };
  const markdown = classifyMarkdown(code);
  if (markdown !== null) return { kind: "markdown", ...markdown };
  return { kind: "python", source: code, meta: {} };
}
