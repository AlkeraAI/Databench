// A spreadsheet, as a table rather than as the commas it is stored in.
//
// The parser is RFC 4180: a field may be quoted, a quoted field may hold the
// delimiter, a newline, or a doubled quote standing for one. Line endings come
// both ways and a file written by a spreadsheet program often starts with a
// byte-order mark, so both are absorbed here rather than surfacing as a stray
// first column.
//
// A large file arrives in windows. The rows parsed so far are the table, under
// the header the first window named; a row the window's edge cut through (a
// quoted field can hold a newline, so a line end is not always a row end) waits
// for the next window rather than being drawn half. The grid pages what is here,
// and the line under it says how much of the file that is. Sorting is a view of
// the rows that have landed, and it is handed to the host so re-opening the file
// lands on the column the reader was using. The press to sort is on the column
// header itself — the header already names the column, so a second row of keys
// above the table was one control too many.

import { useState, type ReactNode } from "react";

import { DataTable } from "../../resources";
import type { PreviewFacts, PreviewProps, PreviewRenderer } from "../types";
import { previewKindFor } from "../kind";
import { MoreWindowBar, useMoreWindow } from "./MoreWindow";

/** Rows per page inside the grid. */
const PAGE_SIZE = 200;

export type SortDirection = "asc" | "desc";

/** Which column the reader sorted by and which way — kept by the host across a
 *  remount, so it arrives as `unknown` and is checked before it is believed. */
export interface CsvSort {
  column: string;
  direction: SortDirection;
}

function claims(facts: PreviewFacts): boolean {
  return previewKindFor(facts) === "csv";
}

/** Split delimited text into rows of fields, RFC 4180. Pure string work: no
 *  type guessing, no trimming, no dropping of empty fields — a cell is exactly
 *  the bytes between its delimiters. */
export function parseDelimited(text: string, delimiter = ","): string[][] {
  return parseDelimitedRows(text, delimiter).rows;
}

/** `parseDelimited`, and whether the text ended between rows. It did not when
 *  the last row has no terminator, or its terminator sits inside a quoted field —
 *  the row is then only as much of itself as has arrived. */
export function parseDelimitedRows(
  text: string,
  delimiter = ",",
): { rows: string[][]; closed: boolean } {
  const source = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  let index = 0;
  const endField = (): void => {
    row.push(field);
    field = "";
  };
  const endRow = (): void => {
    endField();
    rows.push(row);
    row = [];
  };
  while (index < source.length) {
    const char = source[index];
    if (quoted) {
      if (char === '"') {
        if (source[index + 1] === '"') {
          field += '"';
          index += 2;
          continue;
        }
        quoted = false;
        index += 1;
        continue;
      }
      field += char;
      index += 1;
      continue;
    }
    if (char === '"' && field === "") {
      quoted = true;
      index += 1;
      continue;
    }
    if (char === delimiter) {
      endField();
      index += 1;
      continue;
    }
    if (char === "\r" && source[index + 1] === "\n") {
      endRow();
      index += 2;
      continue;
    }
    if (char === "\n" || char === "\r") {
      endRow();
      index += 1;
      continue;
    }
    field += char;
    index += 1;
  }
  // A file ending in a newline has no final row — only a trailing terminator.
  const closed = !(field !== "" || row.length > 0 || quoted);
  if (!closed) endRow();
  return { rows, closed };
}

/** Whether the first row names the columns. A header row is complete and reads
 *  as words; a row of measurements is data whatever it sits above. */
function looksLikeHeader(first: readonly string[]): boolean {
  if (first.length === 0) return false;
  return first.every((cell) => cell.trim() !== "" && !isNumeric(cell));
}

function isNumeric(cell: string): boolean {
  const trimmed = cell.trim();
  return trimmed !== "" && Number.isFinite(Number(trimmed));
}

/** Numbers order as numbers and text as text, so `11` sorts after `3` in a
 *  column of quantities but `item11` still sorts after `item3` by its letters. */
function compareCells(left: string, right: string): number {
  if (isNumeric(left) && isNumeric(right)) return Number(left) - Number(right);
  return left.localeCompare(right);
}

/** The host's remembered sort, when it is one this file can honour. */
function rememberedSort(viewState: unknown, columns: readonly string[]): CsvSort | null {
  if (typeof viewState !== "object" || viewState === null) return null;
  const { column, direction } = viewState as { column?: unknown; direction?: unknown };
  if (typeof column !== "string" || !columns.includes(column)) return null;
  if (direction !== "asc" && direction !== "desc") return null;
  return { column, direction };
}

/** The next sort a press on `column` produces: up, then down, then back to the
 *  order the file itself is in. */
function nextSort(current: CsvSort | null, column: string): CsvSort | null {
  if (current === null || current.column !== column) return { column, direction: "asc" };
  return current.direction === "asc" ? { column, direction: "desc" } : null;
}

export function CsvPreview(props: PreviewProps): ReactNode {
  const tail = useMoreWindow(props.content);
  const text = props.content.kind === "text" ? props.content.text : null;
  const read = text === null ? { rows: [], closed: true } : parseDelimitedRows(text);
  // Past the last landed window a row the edge cut through is half a row.
  const parsed = tail.partial !== null && !read.closed ? read.rows.slice(0, -1) : read.rows;
  const hasHeader = parsed.length > 0 && looksLikeHeader(parsed[0]);
  const width = parsed.reduce((widest, row) => Math.max(widest, row.length), 0);
  const columns = hasHeader
    ? padded(parsed[0], width)
    : Array.from({ length: width }, (_column, index) => `Column ${index + 1}`);
  const body = hasHeader ? parsed.slice(1) : parsed;
  const shown = body.map((row) => padded(row, width));

  const [pressed, setPressed] = useState<CsvSort | null>(null);
  const sort = pressed ?? rememberedSort(props.viewState, columns);
  const rows = sort === null ? shown : sorted(shown, columns.indexOf(sort.column), sort.direction);

  if (text === null) return null;

  const press = (column: string): void => {
    const next = nextSort(sort, column);
    setPressed(next);
    props.onViewState?.(next);
  };

  return (
    <div className="alk-preview-csv" onScrollCapture={tail.onScrollCapture}>
      <DataTable
        columns={columns}
        rows={rows}
        rowNumbers
        fill
        pageSize={PAGE_SIZE}
        emptyLabel="No rows."
        sort={sort}
        onSortColumn={press}
      />
      <MoreWindowBar tail={tail} />
    </div>
  );
}

/** A short row keeps its shape in the grid: a ragged file draws blanks, not a
 *  column that disappears halfway down. */
function padded(row: readonly string[], width: number): string[] {
  const out = [...row];
  while (out.length < width) out.push("");
  return out;
}

function sorted(rows: readonly string[][], column: number, direction: SortDirection): string[][] {
  if (column < 0) return [...rows];
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((left, right) => sign * compareCells(left[column] ?? "", right[column] ?? ""));
}

export const csvRenderer: PreviewRenderer = {
  id: "csv",
  // Above the code and text readers, below markdown: nothing else claims the
  // spreadsheet mime, and the number sits in the same ladder as its neighbours.
  priority: 40,
  match: claims,
  needs: () => "text",
  Component: CsvPreview,
};
