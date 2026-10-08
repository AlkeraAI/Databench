// The grid every tabular result shares: an ordinal gutter, a pinned head, and
// all-numeric columns right-aligned on tabular figures.
import type { ReactElement } from "react";

import "./shared.css";

export type Cell = string | number | boolean | null;
export function readRows(value: unknown): Cell[][] {
  if (!Array.isArray(value)) return [];
  return value.map((row) =>
    (Array.isArray(row) ? row : []).map((cell) => {
      if (cell === null || cell === undefined) return null;
      if (typeof cell === "string" || typeof cell === "number" || typeof cell === "boolean") return cell;
      return JSON.stringify(cell);
    }),
  );
}
function cellText(cell: Cell): string {
  return cell === null ? "" : String(cell);
}
function HeadRow({ columns, numeric }: { columns: string[]; numeric: boolean[] }): ReactElement {
  return (
    <div className="chat-tool-grid__r" role="row">
      <div className="chat-sql-query-n chat-tool-grid__h chat-tool-grid__n" role="columnheader" aria-label="Row" />
      {columns.map((column, index) => (
        <div
          // Keyed by position, not by name: a result may legitimately carry two
          // columns called the same thing (a join's `id`, two aggregates given
          // the same alias), and a repeated key makes React drop one of them.
          key={`${index}-${column}`}
          className="chat-tool-grid__h"
          data-num={numeric[index] ? "" : undefined}
          role="columnheader"
        >
          {column}
        </div>
      ))}
      <div className="chat-tool-grid__h" role="presentation" />
    </div>
  );
}

function DataRow({ row, numeric, ordinal }: { row: Cell[]; numeric: boolean[]; ordinal: number }): ReactElement {
  return (
    <div className="chat-tool-grid__r" role="row">
      <div className="chat-tool-mono chat-tool-grid__d chat-tool-grid__n" role="rowheader">
        {ordinal}
      </div>
      {row.map((cell, c) => (
        <div key={c} className="chat-tool-mono chat-tool-grid__d" data-num={numeric[c] ? "" : undefined} role="cell">
          {cellText(cell)}
        </div>
      ))}
      <div className="chat-tool-grid__d" role="presentation" />
    </div>
  );
}

export function ResultGrid({
  columns,
  rows,
  numeric,
  label = "Query result",
}: {
  columns: string[];
  rows: Cell[][];
  numeric: boolean[];
  /** The grid's accessible name: what the rows are a result of. */
  label?: string;
}): ReactElement {
  // The ordinal gutter, then one track per column, then a free track so a narrow
  // result's row rules still span the well instead of stopping at the last value.
  const template = `max-content ${columns.map(() => "max-content").join(" ")} minmax(0, 1fr)`;
  return (
    <div className="chat-tool-gridwrap">
      <div className="chat-tool-grid" style={{ gridTemplateColumns: template }} role="table" aria-label={label}>
        <HeadRow columns={columns} numeric={numeric} />
        {rows.map((row, r) => (
          <DataRow key={r} row={row} numeric={numeric} ordinal={r + 1} />
        ))}
      </div>
    </div>
  );
}
