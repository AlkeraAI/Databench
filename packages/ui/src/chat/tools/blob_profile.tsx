// blob.profile answers "what shape is this result in" with a fixed-size summary
// per column, however many rows it covers. The card ranks that summary by what a
// reader acts on: what the column holds, whether it has holes, how far its values
// spread, and which values dominate. A column with no holes draws no completeness
// bar, so ink appears exactly where the data is incomplete. Frequency bars are
// measured against the whole result, not against the tallest bar, so a column
// with no dominant value reads as one.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { IconChartHistogram } from "@tabler/icons-react";

import { Text } from "../sharedUi";

import { count, isRecord, num, readResult, records, str } from "./alkeraPayload";
import { HandleBand, shortHandle } from "./blob";
import type { CardHead } from "./step";
import "./shared.css";
import "./blob.css";
import s from "./blob_profile.module.css";

interface TopValue {
  value: unknown;
  count: number;
}

interface ColumnProfile {
  name: string;
  dtype: string;
  nullCount: number;
  /** Null when the column held more distinct values than the profiler counts. */
  distinctCount: number | null;
  distinctCapped: boolean;
  min: unknown;
  max: unknown;
  top: TopValue[];
}

interface ProfileView {
  handle: string;
  topK: number | null;
  rowCount: number;
  columns: ColumnProfile[];
}

function deriveProfile(part: ToolConversationPart): ProfileView {
  const result = readResult(part.output);
  return {
    handle: str(part.input?.handle),
    topK: num(part.input?.top_k),
    rowCount: num(result?.row_count) ?? 0,
    columns: records(result?.columns).map((column) => ({
      name: str(column.name),
      dtype: str(column.dtype),
      nullCount: num(column.null_count) ?? 0,
      distinctCount: num(column.distinct_count),
      distinctCapped: column.distinct_capped === true,
      min: column.min,
      max: column.max,
      top: records(column.top_k).map((entry) => ({ value: entry.value, count: num(entry.count) ?? 0 })),
    })),
  };
}

/** A profiled value as the payload words it. A non-finite number arrives wrapped
 *  so it can survive JSON, and null is a value here rather than a gap. */
function valueText(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (isRecord(value)) return str(value.$nonfinite) || JSON.stringify(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function distinctText(column: ColumnProfile): string {
  if (column.distinctCapped || column.distinctCount === null) return "distinct not counted";
  return `${column.distinctCount.toLocaleString("en-US")} distinct`;
}

/** The share of rows that carry a value, drawn only where a column has holes. */
function Completeness({ present, total }: { present: number; total: number }): ReactElement {
  const share = total > 0 ? Math.max(0, Math.min(1, present / total)) : 0;
  return (
    <span className={s.blobProfileFill} aria-hidden="true">
      <span className={s.blobProfileFillBar} style={{ width: `${share * 100}%` }} />
    </span>
  );
}

/** The most frequent values, each bar measured against the whole result so its
 *  length reads as the share of rows that value covers. */
function TopValues({ values, total }: { values: TopValue[]; total: number }): ReactElement {
  return (
    <div className={s.blobProfileTop}>
      {values.map((entry, index) => (
        <div key={index} className="chat-blob-profile-top__row chat-tool-row">
          <Text className="chat-blob-profile-top__value chat-tool-mono chat-tool-clip" tooltip="truncate">
            {valueText(entry.value)}
          </Text>
          <span className={s.blobProfileTopTrack} aria-hidden="true">
            <span
              className={s.blobProfileTopBar}
              style={{ width: `${total > 0 ? Math.min(1, entry.count / total) * 100 : 0}%` }}
            />
          </span>
          <span className="chat-blob-profile-top__n chat-tool-num chat-tool-quiet">{entry.count.toLocaleString("en-US")}</span>
        </div>
      ))}
    </div>
  );
}

function Column({ column, rowCount }: { column: ColumnProfile; rowCount: number }): ReactElement {
  const ranged = column.min !== null && column.min !== undefined && column.max !== null && column.max !== undefined;
  return (
    <section className={s.blobProfileCol}>
      <p className={`${s.blobProfileColHead} chat-tool-line chat-tool-line--base`}>
        <Text className={`${s.blobProfileColName} chat-tool-mono`} tooltip="truncate">
          {column.name}
        </Text>
        <span className={`${s.blobProfileColDtype} chat-tool-quiet chat-tool-pin`} data-dtype={column.dtype}>
          {column.dtype}
        </span>
        {column.nullCount > 0 ? (
          <span className={s.blobProfileColNull}>{count(column.nullCount, "null", "nulls")}</span>
        ) : null}
        <span
          className="chat-blob-profile-col__distinct chat-tool-num chat-tool-end"
          title={column.distinctCapped ? "More distinct values than the profiler counts" : undefined}
        >
          {distinctText(column)}
        </span>
      </p>
      {column.nullCount > 0 ? <Completeness present={rowCount - column.nullCount} total={rowCount} /> : null}
      {ranged ? (
        <p className={`${s.blobProfileColRange} chat-tool-mono`}>
          <span className={s.blobProfileColLabel}>min</span>
          {valueText(column.min)}
          <span className={s.blobProfileColLabel}>max</span>
          {valueText(column.max)}
        </p>
      ) : null}
      {column.top.length > 0 ? <TopValues values={column.top} total={rowCount} /> : null}
    </section>
  );
}

/** The well's interior: the result profiled, then one block per column. It paints
 *  no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveProfile(part);
  return (
    <div data-tool="blob">
      <HandleBand
        handle={view.handle}
        glyph={<IconChartHistogram size={14} stroke={1.6} />}
        param={view.topK === null ? undefined : `top ${view.topK}`}
      />
      <div className={`${s.blobProfileCols} chat-tool-ruled`} data-cap="340">
        {view.columns.map((column, index) => (
          <Column key={column.name || index} column={column} rowCount={view.rowCount} />
        ))}
      </div>
    </div>
  );
}

/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const view = deriveProfile(part);
  return {
    object: shortHandle(view.handle),
    data: {
      kind: "count",
      text: `${count(view.columns.length, "column", "columns")} over ${count(view.rowCount, "row", "rows")}`,
    },
    body: <Body part={part} />,
  };
};
