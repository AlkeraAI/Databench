// A saved query or a promoted result, and how it came to exist.
//
// The receipt is not a detail behind a click: what a number is worth depends
// entirely on which connection it came from, under which role, when, and how
// long it took — so all of it is on screen with the table. The CSV is a plain
// link to the export route (the session cookie rides the navigation, so nothing
// authenticates in the URL), and a result carrying a chart spec draws it from
// the persisted declaration with the portal's own viz primitives.

import { useMemo, type ReactElement } from "react";
import { Link, useParams } from "react-router-dom";

import { AlkeraChart, formatCount, type FormattedParam } from "@alkera/ui";

import { objectCsvUrl, useObject, useObjectChartRows, useObjectRows } from "../../../api/objects";
import type { WorkspaceObjectRead } from "../../../api/objects";

import { chartCaption, chartSpecOf, rowsForChart } from "./chartSpec";
import { ObjectUnavailable } from "./ObjectUnavailable";
import { displayValue, kindLabel, receiptParams, receiptValue, RECEIPT_FIELDS } from "./receipt";
import "./object-page.css";

/** The parameters a statement was bound with, one row each. A reader checking a
 *  number wants to see which customer and which window produced it — not to
 *  parse `{"customer":"acme"}` off the trust surface. */
function ReceiptParams({ params }: { params: FormattedParam[] }): ReactElement {
  if (params.length === 0) return <>—</>;
  return (
    <ul className="object-page__params">
      {params.map((param) => (
        <li key={param.name}>
          <span className="object-page__param-name">{param.name}</span>
          <span className="object-page__param-value">{param.value}</span>
        </li>
      ))}
    </ul>
  );
}

function receiptOf(object: WorkspaceObjectRead): Record<string, unknown> {
  const receipt = object.spec.receipt;
  return typeof receipt === "object" && receipt !== null ? (receipt as Record<string, unknown>) : {};
}

/** What a result that is not ready says for itself.
 *
 *  A promoted result is created before its rows exist — the machine that ran
 *  the query uploads them — so "saving" is a state a reader meets. When the
 *  machine cannot deliver, it says why, and that reason belongs HERE: the
 *  reader who pressed save is on this page by then, not in the chat, and an
 *  object that only ever says "Saving…" is a page that never resolves.
 *  A result with no rows has nothing to export either, so the CSV key is not
 *  offered until there is a file behind it. */
function statusLine(record: WorkspaceObjectRead): string {
  if (record.status === "pending_upload") {
    return "Saving. The workspace is uploading this result.";
  }
  if (record.status === "failed") {
    const reason = record.spec.failure_reason;
    return typeof reason === "string" && reason
      ? `This result could not be saved: ${reason}.`
      : "This result could not be saved.";
  }
  return "Not ready";
}

export function ObjectPage(): ReactElement {
  const { objectId } = useParams<{ objectId: string }>();
  const object = useObject(objectId);
  // The rows exist only once the machine has delivered them; asking earlier is
  // a 409 in the console for a result that may never arrive.
  const ready = object.data?.status === "ready";
  // The table pages; the chart does not. Read before the early returns because
  // hooks must be, and the spec is on the object either way.
  const chart = chartSpecOf(object.data?.spec.chart_spec);
  const rows = useObjectRows(objectId, 0, undefined, ready);
  const chartRows = useObjectChartRows(objectId, ready && chart !== null);
  const chartData = useMemo(() => (chartRows.data ? rowsForChart(chartRows.data) : null), [chartRows.data]);

  if (object.isError) {
    return <ObjectUnavailable error={object.error} onRetry={() => void object.refetch()} />;
  }
  if (!object.data) return <p className="alk-caption">Loading…</p>;

  const record = object.data;
  const receipt = receiptOf(record);
  const table = rows.data;

  return (
    <div className="object-page">
      <header className="object-page__head">
        <h1 className="alk-h2">{record.title}</h1>
        <span className="object-page__type">{kindLabel(record.type)}</span>
        {record.status !== "ready" ? (
          <span className="object-page__status" data-status={record.status} role="status">
            {statusLine(record)}
          </span>
        ) : null}
        {record.status === "ready" ? (
          <a
            className="alk-btn"
            data-variant="secondary"
            data-fill="outline"
            href={objectCsvUrl(record.id)}
            download
          >
            Export CSV
          </a>
        ) : null}
      </header>

      {chart && chartData ? (
        <section className="object-page__chart" aria-label="Chart">
          {chartCaption(chart) ? <p className="object-page__chart-caption">{chartCaption(chart)}</p> : null}
          <AlkeraChart spec={chart} data={chartData} height={220} ariaLabel={chartCaption(chart) ?? undefined} />
        </section>
      ) : null}

      <section className="object-page__receipt" aria-label="Receipt">
        <dl>
          {RECEIPT_FIELDS.map(({ key, label }) => (
            <div key={key} className="object-page__receipt-row">
              <dt>{label}</dt>
              <dd>
                {key === "params" ? (
                  <ReceiptParams params={receiptParams(receipt)} />
                ) : (
                  receiptValue(receipt, key)
                )}
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {table ? (
        <div className="object-page__table-scroll">
          <table className="alk-table">
            <thead>
              <tr>
                {table.columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{displayValue(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="alk-caption">
            {formatCount(table.rows.length)} of {formatCount(table.total)} rows
          </p>
        </div>
      ) : null}

      <Link className="alk-link" to="/chat">
        Back to chat
      </Link>
    </div>
  );
}
