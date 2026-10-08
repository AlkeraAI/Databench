// blob.query and blob.derive both compute over a result already in hand and both
// answer in the same `SqlQueryResult` shape, so they share one card. The band
// carries the transform: blob.query's statement as written, and blob.derive's
// typed params in the SELECT they compile to, so the two read as the one
// operation they are.
//
// The grid drops the ordinal gutter and the per-column head inks the warehouse
// card needs. Those exist so a value stays attributable across a wide, unfamiliar
// sample; an answer computed over a result the reader has already seen is a few
// rows across a few columns, and the furniture would outweigh it.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { IconAlertTriangle, IconStack2 } from "@tabler/icons-react";

import { Text } from "../sharedUi";

import { paintLeaves, sqlLines } from "../syntax";
import { count, num, numericColumns, objectOutput, readResult, sqlScanLine, str, strings } from "./alkeraPayload";
import { bytes, shortHandle } from "./blob";
import type { CardHead, StepFigure } from "./step";
import { Band } from "./shared";
import "./shared.css";
import "./blob.css";
import s from "./blob_query.module.css";

interface Clause {
  keyword: string;
  argument: string;
}

interface ResultView {
  derived: boolean;
  handle: string;
  sql: string;
  recipe: Clause[];
  columns: string[];
  rows: unknown[][];
  numeric: boolean[];
  rowCount: number;
  truncated: boolean;
  warnings: string[];
  note: string;
  resultName: string;
  spilledHandle: string;
  spilledSize: number;
}

function rowsOf(value: unknown): unknown[][] {
  return Array.isArray(value) ? value.map((row) => (Array.isArray(row) ? (row as unknown[]) : [])) : [];
}

function cellText(cell: unknown): string {
  if (cell === null || cell === undefined) return "";
  if (typeof cell === "object") return JSON.stringify(cell);
  return String(cell);
}

/** blob.derive's typed params in the SQL they compile to, clause by clause. */
function recipeOf(input: Record<string, unknown> | undefined): Clause[] {
  const selected = strings(input?.select_columns);
  const where = str(input?.where);
  const ordered = strings(input?.order_by);
  const limit = num(input?.limit);
  const recipe: Clause[] = [
    {
      keyword: input?.distinct === true ? "select distinct" : "select",
      argument: selected.length > 0 ? selected.join(", ") : "*",
    },
  ];
  if (where) recipe.push({ keyword: "where", argument: where });
  if (ordered.length > 0) {
    recipe.push({
      keyword: "order by",
      argument: `${ordered.join(", ")}${input?.descending === true ? " desc" : ""}`,
    });
  }
  if (limit !== null) recipe.push({ keyword: "limit", argument: String(limit) });
  return recipe;
}

function deriveResult(part: ToolConversationPart): ResultView {
  const result = readResult(part.output);
  const columns = Array.isArray(result?.columns) ? result.columns.map((column) => String(column)) : [];
  const rows = rowsOf(result?.preview_rows);
  const spilled = objectOutput(result?.blob);
  const sql = str(part.input?.sql);
  return {
    derived: part.name.endsWith("blob.derive"),
    handle: str(part.input?.handle),
    sql,
    recipe: recipeOf(part.input),
    columns,
    rows,
    numeric: numericColumns(columns, rows),
    rowCount: num(result?.row_count) ?? rows.length,
    truncated: result?.truncated === true,
    warnings: strings(result?.cost_warnings),
    note: str(result?.note),
    resultName: str(result?.result_name) || str(part.input?.result_name),
    spilledHandle: str(spilled?.sha256),
    spilledSize: num(spilled?.size) ?? 0,
  };
}

/** The transform in the register the tool wrote it in: a statement as the model
 *  typed it, or the reshape's params as the SELECT they compile to. */
function Transform({ view }: { view: ResultView }): ReactElement | null {
  if (view.derived) {
    return (
      <div className="chat-blob-query-recipe chat-tool-mono chat-tool-echo">
        {view.recipe.map((clause) => (
          <p key={clause.keyword} className={s.blobQueryLine}>
            <span className={s.blobQueryKw}>{clause.keyword}</span>
            <span className="chat-blob-query-arg chat-tool-break">{clause.argument}</span>
          </p>
        ))}
      </div>
    );
  }
  if (!view.sql) return null;
  return (
    <div className="chat-blob-query-sql chat-tool-mono chat-tool-echo">
      {sqlLines(view.sql).map((line, index) => (
        <p key={index} className={s.blobQuerySqlLine}>
          {paintLeaves(line)}
        </p>
      ))}
    </div>
  );
}

function Grid({ view }: { view: ResultView }): ReactElement {
  // One track per column, then a free track so a narrow answer's row rules still
  // span the well instead of stopping at the last value.
  const template = `${view.columns.map(() => "max-content").join(" ")} minmax(0, 1fr)`;
  return (
    <div className="chat-blob-query-gridwrap chat-tool-gridwrap">
      <div className="chat-blob-query-grid chat-tool-grid" style={{ gridTemplateColumns: template }} role="table" aria-label="Result">
        <div className="chat-blob-query-r chat-tool-grid__r" role="row">
          {view.columns.map((column, index) => (
            <div
              key={`${column}-${index}`}
              className={`${s.blobQueryH} chat-tool-grid__h`}
              data-num={view.numeric[index] ? "" : undefined}
              role="columnheader"
            >
              {column}
            </div>
          ))}
          <div className={`${s.blobQueryH} chat-tool-grid__h`} role="presentation" />
        </div>
        {view.rows.map((row, r) => (
          <div key={r} className="chat-blob-query-r chat-tool-grid__r" role="row">
            {row.map((cell, c) => (
              <div key={c} className={`${s.blobQueryD} chat-tool-mono chat-tool-grid__d`} data-num={view.numeric[c] ? "" : undefined} role="cell">
                {cellText(cell)}
              </div>
            ))}
            <div className={`${s.blobQueryD} chat-tool-grid__d`} role="presentation" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** The well's interior: the result the transform ran over, the transform, and the
 *  answer it came back with. It paints no ground, edge, radius, or outer pad; the
 *  group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveResult(part);
  return (
    <div data-tool="blob">
      <Band className="chat-blob-query-band chat-tool-band--block">
        <p className="chat-blob-query-head chat-tool-grid-head">
          {view.handle ? (
            <span className={s.blobQuerySrc} title={view.handle}>
              <IconStack2 size={14} stroke={1.7} />
              {shortHandle(view.handle)}
            </span>
          ) : null}
          {view.resultName ? (
            <Text className={s.blobQueryName} tooltip="truncate">
              {view.resultName}
            </Text>
          ) : null}
        </p>
        <Transform view={view} />
      </Band>
      <Grid view={view} />
      {view.warnings.map((warning) => (
        <p key={warning} className="chat-blob-query-warn chat-tool-grid-warn">
          <IconAlertTriangle size={14} stroke={1.8} aria-hidden="true" />
          {warning}
        </p>
      ))}
      {view.spilledHandle ? (
        <p className="chat-blob-query-spill chat-tool-note">
          Stored as{" "}
          <span className="chat-tool-mono" title={view.spilledHandle}>
            {shortHandle(view.spilledHandle)}
          </span>
          , {bytes(view.spilledSize)}, so a later call can query it again.
        </p>
      ) : null}
      {view.note ? <p className="chat-tool-note">{view.note}</p> : null}
    </div>
  );
}

function figureOf(view: ResultView): StepFigure {
  return {
    kind: "count",
    text: `${count(view.rowCount, "row", "rows")}, ${count(view.columns.length, "column", "columns")}`,
  };
}

/** The extent of a grid the well could only sample; a whole answer carries none. */
function footerOf(view: ResultView): string | undefined {
  return view.truncated ? `${view.rows.length} of ${count(view.rowCount, "row", "rows")}` : undefined;
}

/** The step each of the two calls contributes to the transcript's tool group. */
export const headQuery: CardHead = (part) => {
  const view = deriveResult(part);
  return {
    // A query leads with its statement, flattened to one scan line and set
    // whole in the well.
    object: sqlScanLine(view.sql),
    data: figureOf(view),
    body: <Body part={part} />,
    footer: footerOf(view),
  };
};

export const headDerive: CardHead = (part) => {
  const view = deriveResult(part);
  return {
    // A derive names the result it reshaped.
    object: shortHandle(view.handle),
    data: figureOf(view),
    body: <Body part={part} />,
    footer: footerOf(view),
  };
};
