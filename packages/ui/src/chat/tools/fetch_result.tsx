// A page out of a result too large to inline. The blob is the whole thing; this
// call took a window on it, so the card's job is to say which window.
//
// The row gutter therefore counts in the BLOB, not in the page: row 4,801 reads
// as 4,801 whether it arrived first or last. That is the one thing a query
// result's ordinal gutter cannot express, and the reason this grid is its own.
// The head states how much of the total came back and the extent line states
// exactly where it sits, so the two never repeat each other.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { count, isRecord, num, numericColumns, readResult, str } from "./alkeraPayload";
import { Band, EmptyLine } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import "./fetch_result.css";
/** The wire encoding for values JSON cannot carry. */
const NONFINITE: Record<string, string> = { nan: "NaN", inf: "Infinity", "-inf": "-Infinity" };
interface PageView {
  handle: string;
  rows: boolean;
  columns: string[];
  cells: unknown[][];
  numeric: boolean[];
  text: string;
  offset: number;
  limit: number | null;
  total: number;
  returned: number;
  hasMore: boolean;
  nextOffset: number | null;
}
function cellText(cell: unknown): string {
  if (cell === null || cell === undefined) return "";
  if (isRecord(cell) && typeof cell.$nonfinite === "string") return NONFINITE[cell.$nonfinite] ?? cell.$nonfinite;
  if (typeof cell === "object") return JSON.stringify(cell);
  return String(cell);
}
function derivePage(part: ToolConversationPart): PageView {
  const result = readResult(part.output);
  const columns = Array.isArray(result?.columns) ? result.columns.map((column) => String(column)) : [];
  const cells = Array.isArray(result?.rows) ? result.rows.filter(Array.isArray) : [];
  const text = str(result?.text);
  const rows = str(result?.kind) === "rows" || columns.length > 0;
  return {
    handle: str(part.input?.handle) || str(part.input?.blob),
    rows,
    columns,
    cells,
    numeric: numericColumns(columns, cells),
    text,
    offset: num(result?.offset) ?? 0,
    limit: num(part.input?.limit),
    total: num(result?.total) ?? 0,
    returned: num(result?.returned) ?? (rows ? cells.length : text.length),
    hasMore: result?.has_more === true,
    nextOffset: num(result?.next_offset),
  };
}
function Grid({ page }: { page: PageView }): ReactElement {
  // The gutter, one track per column, then a free track so a narrow page's row
  // rules still span the well instead of stopping at the last value.
  const template = `max-content ${page.columns.map(() => "max-content").join(" ")} minmax(0, 1fr)`;
  return (
    <div  data-cap="280">
      <div className="chat-fetch-grid chat-tool-grid" style={{ gridTemplateColumns: template }} role="table" aria-label="Result page">
        <div className="chat-tool-grid__r" role="row">
          <div className="chat-fetch-n chat-tool-grid__h chat-tool-grid__n" role="columnheader" aria-label="Row" />
          {page.columns.map((column, index) => (
            <div key={index} className="chat-tool-grid__h" data-num={page.numeric[index] ? "" : undefined} role="columnheader">
              {column}
            </div>
          ))}
          <div className="chat-tool-grid__h" role="presentation" />
        </div>
        {page.cells.map((row, index) => (
          <div key={index} className="chat-tool-grid__r" role="row">
            <div className="chat-fetch-d chat-fetch-n chat-tool-mono chat-tool-grid__d chat-tool-grid__n" role="rowheader">
              {(page.offset + index + 1).toLocaleString("en-US")}
            </div>
            {page.columns.map((_, cell) => (
              <div key={cell} className="chat-fetch-d chat-tool-mono chat-tool-grid__d" data-num={page.numeric[cell] ? "" : undefined} role="cell">
                {cellText(row[cell])}
              </div>
            ))}
            <div className="chat-fetch-d chat-tool-grid__d" role="presentation" />
          </div>
        ))}
      </div>
    </div>
  );
}
/** The page itself, in whichever of the blob's two shapes it arrived. A window
 *  can land past the end of the blob, so either shape can come back with
 *  nothing in it. */
function Payload({ page }: { page: PageView }): ReactElement {
  if (page.rows) {
    return page.cells.length === 0 ? <EmptyLine>This page has no rows.</EmptyLine> : <Grid page={page} />;
  }
  return page.text.length === 0 ? (
    <EmptyLine>This page has no text.</EmptyLine>
  ) : (
    <pre className="chat-fetch-text chat-tool-mono" data-cap="280">{page.text}</pre>
  );
}
/** The well's interior: the blob the page came from, then the page. It paints no
 *  ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const page = derivePage(part);
  return (
    <div data-tool="fetch_result">
      <Band>
        <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate">
          {page.handle}
        </Text>
        {page.offset > 0 ? <span className="chat-tool-band__param">from {page.offset.toLocaleString("en-US")}</span> : null}
        {page.limit === null ? null : <span className="chat-tool-band__param">limit {page.limit}</span>}
      </Band>
      <Payload page={page} />
    </div>
  );
}
/** The window this page sits in, stated once at the foot of the body. */
function extentOf(page: PageView): string | undefined {
  if (page.total <= 0 || page.returned <= 0) return undefined;
  const unit = page.rows ? "row" : "character";
  const span = `${unit}s ${(page.offset + 1).toLocaleString("en-US")} to ${(page.offset + page.returned).toLocaleString("en-US")} of ${page.total.toLocaleString("en-US")}`;
  if (!page.hasMore || page.nextOffset === null) return span;
  return `${span}, more from ${unit} ${(page.nextOffset + 1).toLocaleString("en-US")}`;
}
/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const page = derivePage(part);
  const one = page.rows ? "row" : "character";
  const many = page.rows ? "rows" : "characters";
  return {
    // The scan line takes the handle short; the band below carries it whole.
    object: page.handle.length > 12 ? `${page.handle.slice(0, 12)}...` : page.handle,
    data:
      page.total > 0
        ? { kind: "count", text: `${page.returned.toLocaleString("en-US")} of ${page.total.toLocaleString("en-US")} ${many}` }
        : { kind: "count", text: count(page.returned, one, many) },
    body: <Body part={part} />,
    footer: extentOf(page),
  };
};
