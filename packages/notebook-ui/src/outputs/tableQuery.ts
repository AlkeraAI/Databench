// The table output's pure parts: the SQL a filter box sends, local sorting
// and filtering when there is no frame to ask, and CSV.

import type { TableField } from "../model/types";

/** A SQL identifier in double quotes, inner quotes doubled. */
export function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

/** A SQL string literal in single quotes, inner quotes doubled. NUL cannot
 *  appear in a literal, so it is dropped. */
export function quoteLiteral(value: string): string {
  // eslint-disable-next-line no-control-regex -- strips NUL
  return `'${value.replace(/\x00/g, "").replace(/'/g, "''")}'`;
}

/** `term` as a LIKE pattern that matches it anywhere, with LIKE's own
 *  wildcards and the escape character taken literally. */
export function containsPattern(term: string): string {
  return `%${term.replace(/[\\%_]/g, (ch) => `\\${ch}`)}%`;
}

/** The `filter_sql` for a filter box: a single SELECT over the table named
 *  `frame` (the one frame the inspection registers) of the rows where
 *  `column` (or any column) contains `term`, case-insensitively. `null` when
 *  there is nothing to filter by. */
export function buildFilterSql(term: string, columns: readonly string[], column: string | null = null): string | null {
  const trimmed = term.trim();
  if (trimmed === "") return null;
  const targets = column === null ? columns : columns.filter((name) => name === column);
  if (targets.length === 0) return null;
  const pattern = quoteLiteral(containsPattern(trimmed));
  const clauses = targets.map((name) => `CAST(${quoteIdent(name)} AS VARCHAR) ILIKE ${pattern} ESCAPE '\\'`);
  const where = clauses.length === 1 ? clauses[0] : clauses.join(" OR ");
  return `SELECT * FROM frame WHERE ${where}`;
}

/** A cell as text, the same way the table shows it. */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** What a cell shows: a binary float rounded to 12 significant digits, so
 *  10267.249999999995 reads 10267.25. The exact value stays in `cellText`,
 *  which filtering and copying use, and in the cell's title. */
export function cellDisplayText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value) && !Number.isInteger(value)) {
    return String(Number(value.toPrecision(12)));
  }
  return cellText(value);
}

/** A column type as short as it reads: "Decimal(precision=4, scale=2)" becomes
 *  "Decimal(4, 2)". The full spelling is the header's title. */
export function shortColumnType(type: string): string {
  return type.replace(/\b[a-z_]+=/g, "");
}

export function filterRowsLocally(
  rows: readonly Record<string, unknown>[],
  term: string,
  columns: readonly string[],
  column: string | null = null,
): Record<string, unknown>[] {
  const needle = term.trim().toLowerCase();
  if (needle === "") return [...rows];
  const targets = column === null ? columns : columns.filter((name) => name === column);
  return rows.filter((row) => targets.some((name) => cellText(row[name]).toLowerCase().includes(needle)));
}

function compareValues(a: unknown, b: unknown): number {
  // Nulls sort last either way the caller flips the rest.
  const aNull = a === null || a === undefined;
  const bNull = b === null || b === undefined;
  if (aNull || bNull) return aNull === bNull ? 0 : aNull ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "bigint" && typeof b === "bigint") return a < b ? -1 : a > b ? 1 : 0;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return cellText(a).localeCompare(cellText(b), undefined, { numeric: true });
}

export function sortRowsLocally(
  rows: readonly Record<string, unknown>[],
  sort: { column: string; descending: boolean } | null,
): Record<string, unknown>[] {
  if (!sort) return [...rows];
  const { column, descending } = sort;
  return [...rows].sort((x, y) => {
    const a = x[column];
    const b = y[column];
    const nullOrder = compareValues(a, b);
    if (a === null || a === undefined || b === null || b === undefined) return nullOrder;
    return descending ? -nullOrder : nullOrder;
  });
}

function csvField(value: string): string {
  return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** RFC 4180 CSV: a header row of the field names, CRLF line ends, fields with
 *  a quote, comma or line break quoted and their quotes doubled. */
export function tableToCsv(fields: readonly TableField[], rows: readonly Record<string, unknown>[]): string {
  const header = fields.map((field) => csvField(field.name)).join(",");
  const body = rows.map((row) => fields.map((field) => csvField(cellText(row[field.name]))).join(","));
  return [header, ...body].join("\r\n") + "\r\n";
}
