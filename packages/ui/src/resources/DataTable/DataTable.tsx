import { useState, type ReactNode } from "react";

import { SortHeader, type SortDirection } from "../../primitives/display/SortHeader";

import { DataTablePager } from "./DataTablePager";
import "./datatable.css";

/** What a sorted cell says to a screen reader. */
const ASCRIPTION = { asc: "ascending", desc: "descending" } as const;

export interface DataTableProps {
  columns: string[];
  /** Pre-formatted cells, row-major. Each row should have one cell per column. */
  rows: ReactNode[][];
  /** Render a leftmost row-number gutter column. */
  rowNumbers?: boolean;
  /** Drop the compact max-height cap so the table fills its scroll parent (the
   *  full blob view); the default keeps the cell-preview height cap. */
  fill?: boolean;
  /** Slice the rows into client-side pages of this size and show a footer pager
   *  (row count + page input + prev/next). Omit (or 0) for a single, un-paged
   *  table — the full blob view pages server-side, so it leaves this unset. */
  pageSize?: number;
  emptyLabel?: string;
  /** Which column the rows are ordered by, and which way. Drawn on the header;
   *  the caller does the ordering. */
  sort?: { column: string; direction: SortDirection } | null;
  /** Make the column headers the sort control, using the shared SortHeader the
   *  rest of the product's tables sort with. A header that names a column is
   *  already the thing a reader reaches for to order by it, so the press lives
   *  there rather than in a row of its own above the table. Omit and the headers
   *  are plain text. */
  onSortColumn?: (column: string) => void;
}

/** The shared data grid behind the SQL result preview and the full blob view: a
 *  sticky-header table with an optional row-number gutter and optional client-side
 *  pagination. Pure presentation — callers format their own cells (records vs row
 *  arrays, non-finite numbers). */
export function DataTable({
  columns,
  rows,
  rowNumbers = false,
  fill = false,
  pageSize,
  emptyLabel = "No rows.",
  sort = null,
  onSortColumn,
}: DataTableProps) {
  const paged = pageSize != null && pageSize > 0;
  const pageCount = paged ? Math.max(1, Math.ceil(rows.length / pageSize)) : 1;
  const [page, setPage] = useState(1);
  // Clamp on render so a shrinking row set can't strand the view past the last page.
  const current = Math.min(Math.max(1, page), pageCount);
  const start = paged ? (current - 1) * pageSize : 0;
  const visible = paged ? rows.slice(start, start + pageSize) : rows;

  if (rows.length === 0 || columns.length === 0) {
    return <div className="alk-datatable__empty">{emptyLabel}</div>;
  }
  const wrapClass = ["alk-datatable__wrap", fill ? "is-fill" : "", paged ? "is-paged" : ""]
    .filter(Boolean)
    .join(" ");
  // `is-fill` on the root as well as the scroller: the grid is a column of two
  // parts, and only a root that takes the height its parent offers can give the
  // table a bounded box to scroll inside and leave the pager below the last row.
  const rootClass = fill ? "alk-datatable is-fill" : "alk-datatable";
  return (
    <div className={rootClass}>
      <div className={wrapClass}>
        <table className="alk-datatable__table">
          <thead>
            <tr>
              {/* Blank header above the row-number gutter — the column header
                  treatment doesn't extend over it. */}
              {rowNumbers ? <th className="alk-datatable__rownum" aria-hidden /> : null}
              {/* Key by index, not the column name — SQL results routinely repeat a
                  name (`SELECT a.id, b.id`), which would collide as a React key. */}
              {columns.map((column, columnIndex) => {
                if (onSortColumn === undefined) return <th key={columnIndex}>{column}</th>;
                const active = sort?.column === column;
                return (
                  // `aria-sort` belongs to the cell, which SortHeader cannot
                  // reach — so the grid sets it here, on the active column only.
                  <th key={columnIndex} aria-sort={active ? ASCRIPTION[sort.direction] : "none"}>
                    <SortHeader
                      active={active}
                      direction={active ? sort.direction : "asc"}
                      onSort={() => onSortColumn(column)}
                    >
                      {column}
                    </SortHeader>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, rowIndex) => (
              // Row numbers count from the page's absolute offset, not 1 each page.
              <tr key={start + rowIndex}>
                {rowNumbers ? <td className="alk-datatable__rownum">{start + rowIndex + 1}</td> : null}
                {columns.map((_column, colIndex) => (
                  <td key={colIndex}>{row[colIndex]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {paged ? (
        <div className="alk-datatable__foot">
          <DataTablePager
            page={current}
            pageCount={pageCount}
            onPage={setPage}
            count={`${rows.length.toLocaleString()} ${rows.length === 1 ? "row" : "rows"}`}
          />
        </div>
      ) : null}
    </div>
  );
}
