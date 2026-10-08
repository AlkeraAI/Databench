import { formatCell, type BlobPage } from "@alkera/chat-model";

import "./blob.css";

/** A plain, read-only table for a page of blob rows. Cells render through
 *  `formatCell` so NaN / ±Infinity show as "NaN" / "∞" / "−∞" rather than a
 *  `{$nonfinite}` wrapper. `maxRows` / `maxCols` bound the compact preview;
 *  omit them for the full view. */
export function BlobTable({
  page,
  maxRows,
  maxCols,
}: {
  page: BlobPage;
  maxRows?: number;
  maxCols?: number;
}) {
  const colCount = maxCols ? Math.min(maxCols, page.columns.length) : page.columns.length;
  const columns = page.columns.slice(0, colCount);
  const rows = maxRows ? page.rows.slice(0, maxRows) : page.rows;
  const moreCols = page.columns.length - columns.length;
  return (
    <table className="alk-blobv-table">
      <thead>
        <tr>
          {columns.map((column, index) => (
            <th key={`${index}-${column}`} scope="col">{column}</th>
          ))}
          {moreCols > 0 ? <th aria-hidden className="alk-blobv-table__more">+{moreCols}</th> : null}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, rowIndex) => (
          <tr key={rowIndex}>
            {columns.map((_column, colIndex) => (
              <td key={colIndex}>{formatCell(row[colIndex])}</td>
            ))}
            {moreCols > 0 ? <td aria-hidden className="alk-blobv-table__more">…</td> : null}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
