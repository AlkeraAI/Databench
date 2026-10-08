// `application/vnd.alkera.table+json`: a virtualized table.
//
// A table whose payload names its source frame pages, sorts and filters
// through `inspectFrame` (the engine's `notebook.inspect frame` read), so it
// reaches every row the kernel holds. Without a name, or without the host's
// read, it sorts and filters the rows it carries.

import { useVirtualizer, type Virtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState } from "react";

import { sentenceStart } from "../model/text";
import type { FrameQuery, TableField, TablePageWire, TablePayload } from "../model/types";
import {
  buildFilterSql,
  cellDisplayText,
  cellText,
  shortColumnType,
  filterRowsLocally,
  sortRowsLocally,
  tableToCsv,
} from "./tableQuery";
import type { OutputRenderer, OutputRendererProps } from "./types";

export type TableSort = { column: string; descending: boolean } | null;

const ROW_HEIGHT = 28;
const FALLBACK_HEIGHT = 360;
const FALLBACK_WIDTH = 800;
const DEFAULT_PAGE = 100;
const MAX_PAGE = 500;
/** How long the filter box waits after typing stops. */
export const FILTER_DEBOUNCE_MS = 250;

/** The viewport the virtualizer fills. A box with no layout yet (hidden, or a
 *  test document) measures as the default size rather than as nothing. */
function observeBox(instance: Virtualizer<HTMLDivElement, Element>, cb: (rect: { width: number; height: number }) => void) {
  const element = instance.scrollElement;
  if (!element) return;
  const measure = () => cb({ width: element.clientWidth || FALLBACK_WIDTH, height: element.clientHeight || FALLBACK_HEIGHT });
  measure();
  const observer = new ResizeObserver(measure);
  observer.observe(element);
  return () => observer.disconnect();
}

function nextSort(current: TableSort, column: string): TableSort {
  if (!current || current.column !== column) return { column, descending: false };
  if (!current.descending) return { column, descending: true };
  return null;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function fieldsOf(list: unknown): TableField[] | null {
  if (!Array.isArray(list)) return null;
  const fields: TableField[] = [];
  for (const f of list) {
    if (!isRecord(f) || typeof f.name !== "string") return null;
    fields.push({ name: f.name, type: typeof f.type === "string" ? f.type : "" });
  }
  return fields;
}

/** Rows as records keyed by column name: the kernel sends each row as a list
 *  of values in column order. */
export function rowRecords(fields: readonly TableField[], rows: readonly unknown[]): Record<string, unknown>[] {
  return rows.map((row) => {
    if (isRecord(row)) return row;
    const values = Array.isArray(row) ? row : [];
    const record: Record<string, unknown> = {};
    fields.forEach((field, i) => {
      record[field.name] = values[i] ?? null;
    });
    return record;
  });
}

/** A table page as the kernel makes it, read the same way wherever it
 *  comes from (a table output's `application/vnd.alkera.table+json`, or a
 *  later, sorted or filtered page from `inspectFrame`):
 *  `{schema: [{name, type}], rows: [[...]], total_rows, offset, source?: {name}}`,
 *  where `source.name` is the global the rows came from, which pages are read
 *  through. A Table Schema payload (`{schema: {fields}, data: [{...}]}`) is
 *  read too. `null` for anything else. */
export function readTablePayload(value: unknown): TablePayload | null {
  if (!isRecord(value)) return null;
  const total = typeof value.total_rows === "number" && Number.isFinite(value.total_rows) ? value.total_rows : null;
  const listed = fieldsOf(value.schema);
  if (listed !== null && Array.isArray(value.rows)) {
    const source = isRecord(value.source) && typeof value.source.name === "string" && value.source.name !== "" ? value.source.name : undefined;
    return {
      ...(source !== undefined ? { name: source } : {}),
      schema: { fields: listed },
      data: rowRecords(listed, value.rows),
      total_rows: total ?? value.rows.length,
    };
  }
  const described = isRecord(value.schema) ? fieldsOf(value.schema.fields) : null;
  if (described !== null && Array.isArray(value.data)) {
    return {
      ...(typeof value.name === "string" && value.name !== "" ? { name: value.name } : {}),
      schema: { fields: described },
      data: rowRecords(described, value.data),
      total_rows: total ?? value.data.length,
    };
  }
  return null;
}

export interface TableViewProps {
  payload: TablePayload;
  inspectFrame?: (query: FrameQuery) => Promise<TablePageWire>;
  /** Where "Copy as CSV" writes; the clipboard by default. */
  writeClipboard?: (text: string) => Promise<void>;
}

/** Said when a page arrives in a shape the table can't read. */
export const UNREADABLE_PAGE = "This page of the table can't be read. Run the cell again.";

const numberFormat = new Intl.NumberFormat("en-US");

export function TableView({ payload, inspectFrame, writeClipboard }: TableViewProps) {
  const fields = payload.schema.fields;
  const columns = useMemo(() => fields.map((field) => field.name), [fields]);
  const remote = Boolean(payload.name && inspectFrame);
  const pageSize = payload.data.length > 0 ? Math.min(payload.data.length, MAX_PAGE) : DEFAULT_PAGE;

  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<TableSort>(null);
  const [filterInput, setFilterInput] = useState("");
  const [filterTerm, setFilterTerm] = useState("");
  const [filterColumn, setFilterColumn] = useState<string | null>(null);
  const [fetched, setFetched] = useState<TablePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const request = useRef(0);

  useEffect(() => {
    if (filterInput === filterTerm) return;
    const timer = setTimeout(() => {
      setFilterTerm(filterInput);
      setPage(0);
    }, FILTER_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [filterInput, filterTerm]);

  const filterSql = useMemo(() => buildFilterSql(filterTerm, columns, filterColumn), [filterTerm, columns, filterColumn]);
  // The first page, unsorted and unfiltered, is the payload's own rows,
  // unless the kernel sent the schema alone.
  const payloadCovers = payload.data.length > 0 || payload.total_rows === 0;
  const usesPayload = page === 0 && sort === null && filterSql === null && payloadCovers;

  useEffect(() => {
    if (!remote || usesPayload || !inspectFrame || !payload.name) {
      setFetched(null);
      setLoading(false);
      return;
    }
    const id = ++request.current;
    const query: FrameQuery = { name: payload.name, offset: page * pageSize, limit: pageSize };
    if (sort) query.sort = [sort];
    if (filterSql) query.filter_sql = filterSql;
    setLoading(true);
    setError(null);
    inspectFrame(query).then(
      (result) => {
        if (id !== request.current) return;
        setLoading(false);
        // Every page is read the way the output's own first page is.
        const read = readTablePayload(result);
        if (read === null) setError(UNREADABLE_PAGE);
        else setFetched(read);
      },
      (reason: unknown) => {
        if (id !== request.current) return;
        setLoading(false);
        // A refusal is the server's own words, shown as a sentence of its own.
        setError(sentenceStart(reason instanceof Error ? reason.message : String(reason)));
      },
    );
  }, [remote, usesPayload, inspectFrame, payload.name, page, pageSize, sort, filterSql]);

  const local = useMemo(
    () => (remote ? [] : sortRowsLocally(filterRowsLocally(payload.data, filterTerm, columns, filterColumn), sort)),
    [remote, payload.data, filterTerm, columns, filterColumn, sort],
  );

  let rows: Record<string, unknown>[];
  let total: number;
  let offset = 0;
  if (!remote) {
    rows = local;
    total = filterTerm.trim() ? local.length : payload.total_rows;
  } else if (usesPayload) {
    rows = payload.data;
    total = payload.total_rows;
  } else {
    rows = fetched?.data ?? [];
    total = fetched?.total_rows ?? payload.total_rows;
    offset = page * pageSize;
  }
  const pages = remote ? Math.max(1, Math.ceil(total / pageSize)) : 1;

  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
    observeElementRect: observeBox,
    initialRect: { width: FALLBACK_WIDTH, height: FALLBACK_HEIGHT },
  });
  const items = virtualizer.getVirtualItems();
  const padTop = items.length ? items[0].start : 0;
  const padBottom = items.length ? virtualizer.getTotalSize() - items[items.length - 1].end : 0;

  const copy = async () => {
    const csv = tableToCsv(fields, rows);
    const write = writeClipboard ?? ((text: string) => navigator.clipboard.writeText(text));
    try {
      await write(csv);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Could not copy to the clipboard");
    }
  };

  let summary: string;
  if (!remote && filterTerm.trim()) summary = `${numberFormat.format(total)} of ${numberFormat.format(payload.data.length)} rows`;
  else if (!remote && payload.total_rows > payload.data.length)
    summary = `${numberFormat.format(payload.data.length)} of ${numberFormat.format(payload.total_rows)} rows shown`;
  else summary = `${numberFormat.format(total)} ${total === 1 ? "row" : "rows"}`;

  return (
    <div className="nb-output-table">
      <div className="nb-table-toolbar">
        <input
          type="search"
          className="nb-table-filter"
          placeholder="Filter"
          aria-label="Filter rows"
          value={filterInput}
          onChange={(event) => setFilterInput(event.target.value)}
        />
        <select
          className="nb-table-filter-column"
          aria-label="Filter column"
          value={filterColumn ?? ""}
          onChange={(event) => {
            setFilterColumn(event.target.value === "" ? null : event.target.value);
            setPage(0);
          }}
        >
          <option value="">All columns</option>
          {columns.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <span className="nb-table-total" aria-live="polite">
          {summary}
        </span>
        <button type="button" className="nb-table-action" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy as CSV"}
        </button>
      </div>
      {error && (
        <p className="nb-output-note nb-output-note--error" role="alert">
          {error}
        </p>
      )}
      <div className="nb-table-scroll" ref={scrollRef} aria-busy={loading}>
        <table>
          <thead>
            <tr>
              {fields.map((field) => {
                const active = sort?.column === field.name ? sort : null;
                return (
                  <th
                    key={field.name}
                    scope="col"
                    aria-sort={active ? (active.descending ? "descending" : "ascending") : "none"}
                  >
                    <button
                      type="button"
                      className="nb-table-sort"
                      onClick={() => {
                        setSort((current) => nextSort(current, field.name));
                        setPage(0);
                      }}
                    >
                      <span className="nb-table-column">{field.name}</span>
                      <span className="nb-table-type" title={field.type}>
                        {shortColumnType(field.type)}
                      </span>
                      <span className="nb-table-sort-mark" aria-hidden="true">
                        {active ? (active.descending ? "↓" : "↑") : ""}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {padTop > 0 && (
              <tr aria-hidden="true" style={{ height: padTop }}>
                <td colSpan={fields.length} />
              </tr>
            )}
            {items.map((item) => {
              const row = rows[item.index];
              return (
                <tr key={item.key} data-row={offset + item.index} style={{ height: ROW_HEIGHT }}>
                  {fields.map((field) => {
                    const value = row[field.name];
                    const empty = value === null || value === undefined;
                    return (
                      <td
                        key={field.name}
                        className={empty ? "nb-table-null" : undefined}
                        title={empty ? undefined : cellText(value)}
                      >
                        {empty ? "null" : cellDisplayText(value)}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {padBottom > 0 && (
              <tr aria-hidden="true" style={{ height: padBottom }}>
                <td colSpan={fields.length} />
              </tr>
            )}
          </tbody>
        </table>
        {rows.length === 0 && !loading && <p className="nb-output-note">No rows</p>}
      </div>
      {remote && pages > 1 && (
        <div className="nb-table-pager">
          <button type="button" className="nb-table-action" disabled={page === 0 || loading} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <PageInput page={page} pages={pages} onPage={setPage} />
          <button
            type="button"
            className="nb-table-action"
            disabled={page + 1 >= pages || loading}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

/** "Page [n] of N": the number is typed, and taken on Enter or blur. A
 *  number outside 1..N is clamped; anything else puts the page back. */
function PageInput({ page, pages, onPage }: { page: number; pages: number; onPage: (page: number) => void }) {
  const [draft, setDraft] = useState(String(page + 1));
  useEffect(() => setDraft(String(page + 1)), [page]);
  const commit = () => {
    const typed = Number.parseInt(draft, 10);
    if (Number.isNaN(typed)) {
      setDraft(String(page + 1));
      return;
    }
    const next = Math.min(Math.max(typed, 1), pages) - 1;
    setDraft(String(next + 1));
    if (next !== page) onPage(next);
  };
  return (
    <label className="nb-table-page">
      Page{" "}
      <input
        type="text"
        inputMode="numeric"
        className="nb-table-page-input"
        aria-label="Page number"
        size={Math.max(2, String(pages).length)}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") commit();
        }}
      />{" "}
      of {numberFormat.format(pages)}
    </label>
  );
}

function TableOutput({ data, context }: OutputRendererProps) {
  const payload = useMemo(() => readTablePayload(data), [data]);
  const { inspectFrame, cellId } = context;
  // Pages are asked for by the cell whose output this is.
  const inspect = useMemo(
    () => (inspectFrame ? (query: FrameQuery) => inspectFrame({ ...query, cell_id: cellId }) : undefined),
    [inspectFrame, cellId],
  );
  if (payload === null) return <p className="nb-output-note">This table could not be read.</p>;
  return <TableView payload={payload} inspectFrame={inspect} />;
}

export const tableRenderer: OutputRenderer = {
  id: "alkera.table",
  mimes: ["application/vnd.alkera.table+json"],
  rank: 0,
  place: "app",
  Component: TableOutput,
};
