// The SQL statement vocabulary the sql card colors. Operators and punctuation
// stay plain ink: the statement treatment rations chroma to the words that
// carry meaning.

import { tokenLines, type SyntaxLeaf } from "./leaves";

export type SqlKind = "keyword" | "function" | "string" | "number" | "comment";

const SQL_KIND: Record<string, SqlKind> = {
  keyword: "keyword",
  boolean: "keyword",
  function: "function",
  string: "string",
  char: "string",
  number: "number",
  comment: "comment",
};

/** Prism's SQL grammar kinds `function` off a 14-name allowlist
 *  (AVG|COUNT|FIRST|FORMAT|LAST|LCASE|LEN|MAX|MID|MIN|MOD|NOW|ROUND|SUM|UCASE),
 *  so every warehouse call head outside it arrives bare. A call head is an
 *  identifier the grammar left unkinded, ending its leaf, with the paren
 *  opening the next one. A space between the two makes it a name the grammar
 *  already read: `INSERT INTO orders (order_id)` names a table, not a call. */
const CALL_HEAD = /^(\s*)([A-Za-z_][A-Za-z0-9_$]*)$/;

function withCallHeads(line: SyntaxLeaf<SqlKind>[]): SyntaxLeaf<SqlKind>[] {
  const out: SyntaxLeaf<SqlKind>[] = [];
  for (let i = 0; i < line.length; i += 1) {
    const leaf = line[i];
    const next = line[i + 1];
    const head =
      leaf.kind === null && next !== undefined && next.text.startsWith("(") ? CALL_HEAD.exec(leaf.text) : null;
    if (head === null) {
      out.push(leaf);
      continue;
    }
    if (head[1] !== "") out.push({ text: head[1], kind: null });
    out.push({ text: head[2], kind: "function" });
  }
  return out;
}

export function sqlLines(code: string): SyntaxLeaf<SqlKind>[][] {
  return tokenLines(code, "sql", SQL_KIND).map(withCallHeads);
}
