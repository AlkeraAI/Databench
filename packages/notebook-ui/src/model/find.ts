// Find and replace across a notebook's cells. Sources are searched and can be
// replaced; outputs are searched through their plain text and stream text and
// are never written. Offsets are UTF-16 code units, as CodeMirror counts them.

import type { CellOutput, CellRuntime, DocCell, ReplaceCellOp } from "./types";

export interface FindScope {
  /** Python, SQL and every other non-Markdown cell's source. */
  code: boolean;
  markdown: boolean;
  outputs: boolean;
}

export interface FindOptions {
  query: string;
  caseSensitive?: boolean;
  wholeWord?: boolean;
  regex?: boolean;
  scope: FindScope;
}

export interface FindMatch {
  cell_id: string;
  where: "source" | "output";
  /** The output searched, for a match in an output. */
  output_id?: string;
  from: number;
  to: number;
  /** 1-based line of `from` in the searched text. */
  line: number;
}

export type FindError = { kind: "invalid_regex"; message: string };
export type FindResult = { ok: true; matches: FindMatch[] } | { ok: false; error: FindError };

export type ReplaceError =
  | FindError
  /** Outputs are what the kernel produced; replace never writes them. */
  | { kind: "output_read_only" }
  /** The match no longer exists in the cell's current text. */
  | { kind: "stale_match" };

export type ReplaceResult = { ok: true; ops: ReplaceCellOp[]; count: number } | { ok: false; error: ReplaceError };

type Runtimes = Readonly<Record<string, Pick<CellRuntime, "outputs"> | undefined>>;

interface Hit {
  from: number;
  to: number;
  exec: RegExpExecArray;
}

const WORD = "[\\p{L}\\p{N}_]";

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The search as a global regular expression, null for an empty query. */
export function compileQuery(options: Pick<FindOptions, "query" | "caseSensitive" | "wholeWord" | "regex">): RegExp | null | FindError {
  if (options.query === "") return null;
  let source = options.regex ? options.query : escapeRegExp(options.query);
  if (options.wholeWord) source = `(?<!${WORD})(?:${source})(?!${WORD})`;
  try {
    return new RegExp(source, options.caseSensitive ? "gu" : "giu");
  } catch (err) {
    return { kind: "invalid_regex", message: err instanceof Error ? err.message : String(err) };
  }
}

function scan(text: string, re: RegExp): Hit[] {
  const hits: Hit[] = [];
  re.lastIndex = 0;
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    if (m[0].length === 0) {
      // An empty match finds nothing to show; step one code point past it.
      const cp = text.codePointAt(m.index);
      re.lastIndex = m.index + (cp !== undefined && cp > 0xffff ? 2 : 1);
      continue;
    }
    hits.push({ from: m.index, to: m.index + m[0].length, exec: m });
  }
  return hits;
}

function lineAt(text: string, offset: number): number {
  let line = 1;
  for (let i = text.indexOf("\n"); i !== -1 && i < offset; i = text.indexOf("\n", i + 1)) line++;
  return line;
}

function inScope(cell: DocCell, scope: FindScope): boolean {
  return cell.kind === "markdown" ? scope.markdown : scope.code;
}

/** The text an output is searched through: `text/plain` or a stream's text. */
export function outputText(output: CellOutput): string | null {
  if (output.type === "stream") return output.text;
  if (output.type === "display") {
    const plain = output.data["text/plain"];
    if (typeof plain === "string") return plain;
    if (Array.isArray(plain) && plain.every((part) => typeof part === "string")) return plain.join("");
  }
  return null;
}

/** Every match, cell by cell in document order: the source first, then each
 *  output in order. */
export function findMatches(cells: readonly DocCell[], runtime: Runtimes, options: FindOptions): FindResult {
  const re = compileQuery(options);
  if (re === null) return { ok: true, matches: [] };
  if (!(re instanceof RegExp)) return { ok: false, error: re };
  const matches: FindMatch[] = [];
  for (const cell of cells) {
    if (inScope(cell, options.scope)) {
      for (const hit of scan(cell.source, re)) {
        matches.push({ cell_id: cell.id, where: "source", from: hit.from, to: hit.to, line: lineAt(cell.source, hit.from) });
      }
    }
    if (options.scope.outputs) {
      for (const output of runtime[cell.id]?.outputs ?? []) {
        const text = outputText(output);
        if (text === null) continue;
        for (const hit of scan(text, re)) {
          matches.push({
            cell_id: cell.id,
            where: "output",
            output_id: output.output_id,
            from: hit.from,
            to: hit.to,
            line: lineAt(text, hit.from),
          });
        }
      }
    }
  }
  return { ok: true, matches };
}

/** `$$`, `$&`, `$1`..`$99` and `$<name>`, as `String.prototype.replace` reads
 *  them; any other `$` stays as written. */
export function expandReplacement(template: string, exec: RegExpExecArray): string {
  const groups = exec.length - 1;
  return template.replace(/\$(\$|&|<([^>]*)>|(\d{1,2}))/g, (whole, token: string, name: string | undefined, digits: string | undefined) => {
    if (token === "$") return "$";
    if (token === "&") return exec[0];
    if (name !== undefined) return exec.groups ? (exec.groups[name] ?? "") : whole;
    if (digits !== undefined) {
      const two = Number(digits);
      if (digits.length === 2 && two >= 1 && two <= groups) return exec[two] ?? "";
      const one = Number(digits[0]);
      if (one >= 1 && one <= groups) return (exec[one] ?? "") + digits.slice(1);
      return whole;
    }
    return whole;
  });
}

function splice(source: string, hits: readonly Hit[], replacement: string, regex: boolean): string {
  let out = source;
  for (const hit of [...hits].sort((a, b) => b.from - a.from)) {
    const text = regex ? expandReplacement(replacement, hit.exec) : replacement;
    out = out.slice(0, hit.from) + text + out.slice(hit.to);
  }
  return out;
}

/** Replace every source match in scope; one `replace` operation per changed
 *  cell. Outputs in scope are skipped, never written. */
export function replaceAll(cells: readonly DocCell[], options: FindOptions, replacement: string): ReplaceResult {
  const re = compileQuery(options);
  if (re === null) return { ok: true, ops: [], count: 0 };
  if (!(re instanceof RegExp)) return { ok: false, error: re };
  const ops: ReplaceCellOp[] = [];
  let count = 0;
  for (const cell of cells) {
    if (!inScope(cell, options.scope)) continue;
    const hits = scan(cell.source, re);
    if (hits.length === 0) continue;
    const source = splice(cell.source, hits, replacement, options.regex === true);
    count += hits.length;
    if (source !== cell.source) ops.push({ op: "replace", cell_id: cell.id, source });
  }
  return { ok: true, ops, count };
}

/** Replace one match found by `findMatches`, checked against the cell's
 *  current text first. */
export function replaceOne(cells: readonly DocCell[], match: FindMatch, options: FindOptions, replacement: string): ReplaceResult {
  if (match.where === "output") return { ok: false, error: { kind: "output_read_only" } };
  const re = compileQuery(options);
  if (!(re instanceof RegExp)) return re === null ? { ok: false, error: { kind: "stale_match" } } : { ok: false, error: re };
  const cell = cells.find((c) => c.id === match.cell_id);
  const hit = cell ? scan(cell.source, re).find((h) => h.from === match.from && h.to === match.to) : undefined;
  if (!cell || !hit) return { ok: false, error: { kind: "stale_match" } };
  const source = splice(cell.source, [hit], replacement, options.regex === true);
  return { ok: true, ops: source === cell.source ? [] : [{ op: "replace", cell_id: cell.id, source }], count: 1 };
}
