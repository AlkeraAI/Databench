// Reading a tool's result off the wire, shared by every tool body. The unwrap
// itself belongs to the model package, where the card registry and the webview's
// event fold read it too; these are the narrowings a card body wants. Every
// reader is defensive: a payload shaped differently yields nothing rather than
// throwing, so a body renders its empty state instead of taking down the step.

import { isRecord, parseRecord, toolResultRecord } from "@alkera/chat-model";

import { formatCount } from "../../primitives/format";

export { isRecord };

/** Unwrap a tool result to the object the tool actually returned. */
export const readResult = toolResultRecord;

/** A tool output as the object it is, parsing a JSON string but never unwrapping
 *  an envelope -- for payloads whose top-level keys ARE the answer. */
export const objectOutput = parseRecord;

export function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Resolve a tool's target file across the harnesses' input spellings. */
export function toolPath(input: Record<string, unknown> | undefined): string {
  const value =
    input?.path ?? input?.filePath ?? input?.file_path ?? input?.file;
  return typeof value === "string" ? value : "";
}

export function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/** A statement as a head's one scan line: leading comment lines dropped, the
 *  rest flattened. The head has one clipped line to spend, and a comment spends
 *  it on words that are not the query; the well still sets the statement whole. */
export function sqlScanLine(sql: string): string {
  return sql
    .split("\n")
    .filter((line) => !/^\s*--/.test(line))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Plural agreement for a payload figure: `3 rows`, `1 row`, `1,234,567 rows`.
 *  The digits are grouped by the shared reading, so a count in a card and the
 *  same count on the receipt of the result it was saved into never differ. */
export function count(n: number, one: string, many: string): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];
/** Bytes at the scale a reader thinks in. */
export function bytes(size: number): string {
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? String(Math.round(value)) : value.toFixed(value < 10 ? 1 : 0)} ${BYTE_UNITS[unit]}`;
}

/** Group a run of results by the key they share, without reordering them: a
 *  tool's own ordering is part of its answer, so the grouping only ever folds
 *  neighbors. A key that reappears after an interruption opens a second group,
 *  which is the truthful reading of what the tool returned. */
export function groupRuns<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
): { key: string; items: T[] }[] {
  const groups: { key: string; items: T[] }[] = [];
  for (const item of items) {
    const key = keyOf(item);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, items: [item] });
  }
  return groups;
}

/** How many of each kind a result holds, most numerous first. Ties keep the
 *  order the payload introduced them in, so a re-run of the same answer reads
 *  the same way. */
export function tally<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
): [string, number][] {
  const counts = new Map<string, number>();
  for (const item of items) {
    const key = keyOf(item);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1]);
}

/** A column reads as numeric only when every value in it is a number, so one text
 *  cell keeps the whole column left aligned instead of half-aligning it. */
export function numericColumns(
  columns: string[],
  rows: readonly unknown[][],
): boolean[] {
  return columns.map(
    (_, index) =>
      rows.length > 0 && rows.every((row) => typeof row[index] === "number"),
  );
}

/** Read one `Key: value` line, matched without case. */
export function field(lines: string[], key: string): string {
  const needle = `${key}:`.toLowerCase();
  for (const line of lines) {
    const colon = line.indexOf(":");
    if (
      colon !== -1 &&
      line
        .slice(0, colon + 1)
        .trim()
        .toLowerCase() === needle
    ) {
      return line.slice(colon + 1).trim();
    }
  }
  return "";
}
