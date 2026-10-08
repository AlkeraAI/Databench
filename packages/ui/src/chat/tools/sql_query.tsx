// A SQL call as the two things a query is: the statement, and the grid it came
// back with. The statement is numbered so a warning that cites a line lands
// somewhere, and carries the connection and limit that shaped it. The grid
// keeps an ordinal gutter, pins its head, and right-aligns all-numeric columns
// on tabular figures, in the same neutral head treatment every result grid
// shares.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text, formatDuration, formatTimestamp } from "../sharedUi";
import { IconAlertTriangle, IconDatabase } from "@tabler/icons-react";
import { paintLeaves, sqlLines } from "../syntax";
import { count, isRecord, num, numericColumns, readResult, sqlScanLine, str, strings } from "./alkeraPayload";
import { ResultGrid, readRows, type Cell } from "./resultGrid";
import type { CardHead } from "./step";
import { Band, EmptyLine } from "./shared";
import "./shared.css";
import s from "./sql_query.module.css";
interface QueryView {
  columns: string[];
  rows: Cell[][];
  numeric: boolean[];
  rowCount: number;
  truncated: boolean;
  warnings: string[];
  sql: string;
  /** The relation a statement-less call read whole (`table` mode). */
  table: string;
  connection: string;
  limit: number | null;
  resultName: string;
  note: string;
  /** Which engine actually ran it — two connections of different kinds read
   *  identically without it. */
  engine: string;
  /** The credential role the statement ran under: the same connection can be
   *  read as an analyst or as an owner, and the reader is owed which. */
  role: string;
  /** When it ran, and how long it took: a number without either is a number
   *  nobody can date. */
  executedAt: string;
  durationMs: number | null;
}
function deriveQuery(part: ToolConversationPart): QueryView {
  const result = readResult(part.output);
  const columns = Array.isArray(result?.columns) ? result.columns.map((column) => String(column)) : [];
  const rows = readRows(result?.preview_rows);
  // The tool stamps an executed statement with its provenance block; a result
  // recorded before it did may carry the same facts flat, or not at all.
  const provenance: Record<string, unknown> = isRecord(result?.provenance) ? result.provenance : {};
  return {
    columns,
    rows,
    numeric: numericColumns(columns, rows),
    rowCount: num(provenance.row_count) ?? num(result?.row_count) ?? rows.length,
    truncated: result?.truncated === true,
    warnings: strings(result?.cost_warnings),
    // The echo is the statement the call WROTE; a table-mode call wrote none
    // (its compiled read is the receipt's business, not the card's).
    sql: str(part.input?.sql) || str(part.input?.query),
    table: str(part.input?.table),
    connection: str(part.input?.connection) || str(provenance.connection_name),
    limit: num(part.input?.limit),
    resultName: str(result?.result_name) || str(part.input?.result_name),
    note: str(result?.note),
    engine: str(provenance.engine) || str(result?.engine) || str(part.input?.engine),
    role: str(provenance.role) || str(result?.role),
    executedAt: str(provenance.executed_at) || str(result?.executed_at),
    durationMs: num(provenance.duration_ms) ?? num(result?.duration_ms),
  };
}

/** The reader's own clock, to the second, zone named: a result's age is the
 *  difference between when it ran and now, so the time has to be readable — and
 *  the same reading the receipt of this result shows, since the two are meant to
 *  be compared. */
function runLabel(executedAt: string): string {
  return executedAt ? formatTimestamp(executedAt) : "";
}
/** The well's interior: the statement over the grid it returned, then whatever
 *  the backend attached. It paints no ground, edge, radius, or outer pad; the
 *  group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const {
    columns,
    rows,
    numeric,
    rowCount,
    warnings,
    sql,
    table,
    connection,
    limit,
    resultName,
    note,
    engine,
    role,
    executedAt,
    durationMs,
  } = deriveQuery(part);
  const statement = sqlLines(sql);
  const failed = part.state === "error";
  return (
    <div data-tool="sql_query">
      <Band className="chat-tool-band--block">
        <p className="chat-tool-grid-head">
          {/* The source, whole: which connection, on which engine, under which
              role. Two connections of different kinds — or one connection read
              as two different roles — must not read identically. */}
          <span className={s.sqlQueryConn} data-source="">
            <IconDatabase size={14} stroke={1.7} />
            {connection}
            {engine ? <span className={s.sqlQuerySource}>· {engine}</span> : null}
            {role ? <span className={s.sqlQuerySource}>· as {role}</span> : null}
          </span>
          {resultName ? (
            <Text className={s.sqlQueryResult} tooltip="truncate">
              {resultName}
            </Text>
          ) : null}
          {limit === null ? null : <span className="chat-tool-num chat-tool-end">limit {limit}</span>}
        </p>
        {/* The provenance line: when, how big and how long. It is here rather
            than behind a disclosure because a number whose source you have to
            go looking for is a number you cannot use. */}
        <p className="chat-tool-grid-head" data-provenance="">
          {executedAt ? <span className="chat-tool-num">{runLabel(executedAt)}</span> : null}
          {failed ? null : <span className="chat-tool-num">{count(rowCount, "row", "rows")}</span>}
          {durationMs === null ? null : (
            <span className="chat-tool-num">{formatDuration(durationMs)}</span>
          )}
        </p>
        {/* The echo is what the call ran: the numbered statement, or the one
            relation a table-mode call read whole. No SQL and no table means
            nothing ran to echo, so nothing renders. */}
        {sql ? (
          <div className="chat-tool-mono chat-tool-echo">
            {statement.map((line, index) => (
              <p key={index} className={s.sqlQueryLine}>
                <span className={s.sqlQueryNo} aria-hidden="true">
                  {index + 1}
                </span>
                <span className="chat-tool-band__text">{paintLeaves(line)}</span>
              </p>
            ))}
          </div>
        ) : table ? (
          <p className="chat-tool-mono chat-tool-echo">{table}</p>
        ) : null}
      </Band>
      {/* A call that failed produced no result: its reason is the group's error
          line, and a count or an empty-result caption here would describe a
          result that never existed. */}
      {failed ? null : columns.length > 0 ? (
        <ResultGrid columns={columns} rows={rows} numeric={numeric} />
      ) : (
        <EmptyLine>
          {rowCount > 0 ? "No preview rows came back with the result." : "The query returned no rows."}
        </EmptyLine>
      )}
      {warnings.map((warning, index) => (
        <p key={`${index}-${warning}`} className="chat-tool-grid-warn">
          <IconAlertTriangle size={14} stroke={1.8} />
          {warning}
        </p>
      ))}
      {note ? <p className="chat-tool-note">{note}</p> : null}
    </div>
  );
}
/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const { columns, rowCount, truncated, sql, table } = deriveQuery(part);
  return {
    // A table-mode call ran no statement, so its relation is the object.
    object: sqlScanLine(sql) || table,
    data:
      part.state === "error"
        ? { kind: "count", text: "Failed" }
        : {
            kind: "count",
            text: `${count(rowCount, "row", "rows")}${truncated ? ", truncated" : ""}, ${count(columns.length, "column", "columns")}`,
          },
    body: <Body part={part} />,
  };
};
