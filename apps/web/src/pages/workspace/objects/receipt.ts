// Reading a receipt — the nine facts that decide whether a number is worth
// acting on.
//
// The keys here are the ones the SERVER persists, spelled exactly as
// `alkera_core.schemas.objects.Receipt` declares them. A misspelled key renders
// as a dash with no warning, because a missing key and an unknown value look
// identical to a renderer.
//
// `principal_chain` is an actor document, not a name: `{acting, chain,
// delegating_user}` as the authz package persists it, wrapped under
// `promoted_by` when the cloud wrote it at promote time. A reader does not want
// the document — they want to know who asked, and through what. So it is
// summarised as "the person via the agent", and anything that is not an actor
// document is shown verbatim rather than silently dropped.

import {
  formatCount,
  formatDuration,
  formatParams,
  formatTimestamp,
  type FormattedParam,
} from "@alkera/ui";

export type { FormattedParam };

/** How each kind reads. The object's own `type` is the wire's word; a reader
 *  gets the noun they saved it as. Spelled once, for every surface that names
 *  an object. */
const KIND_LABEL: Record<string, string> = { result: "Result", query: "Query", report: "Report" };

export function kindLabel(type: string): string {
  return KIND_LABEL[type] ?? type;
}

/** The engine is stored as the driver's id ("tinybird", "postgres"); a reader
 *  gets it as a name. */
export function engineLabel(engine: string): string {
  return engine ? `${engine[0].toUpperCase()}${engine.slice(1)}` : engine;
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** The receipt fields, in the order they answer "can I trust this number?".
 *  All nine are on screen with the table — never behind a disclosure. */
export const RECEIPT_FIELDS: readonly { key: string; label: string }[] = [
  { key: "sql", label: "SQL" },
  { key: "connection_name", label: "Connection" },
  { key: "role", label: "Role" },
  { key: "engine", label: "Engine" },
  { key: "principal_chain", label: "Run by" },
  { key: "executed_at", label: "Executed" },
  { key: "row_count", label: "Rows" },
  // The unit rides the value (`412 ms`, `1.2 s`), so the label stops carrying
  // one the reader then has to convert from.
  { key: "duration_ms", label: "Duration" },
  { key: "params", label: "Parameters" },
];

/** How this page renders a value it did not type — a receipt field or a table
 *  cell. Null is the unknown, and says so. An empty string is NOT: in a cell it
 *  is a real value the query returned, so it is left as it is. */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number") return String(value);
  return JSON.stringify(value);
}

/** On a receipt, "" / {} / [] mean the same as null: the machine sent nothing.
 *  A dash is the honest rendering of that, and it is what an older receipt —
 *  written before the schema admitted null — left behind. */
function receiptUnknown(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return isRecord(value) && Object.keys(value).length === 0;
}

function nameOf(principal: Record<string, unknown>): string {
  const label = principal.label;
  const id = principal.id;
  if (typeof label === "string" && label) return label;
  return typeof id === "string" ? id : "";
}

function linksOf(record: Record<string, unknown>): Record<string, unknown>[] {
  return Array.isArray(record.chain) ? record.chain.filter(isRecord) : [];
}

/** The actor document inside `principal_chain`, wherever it sits: at the top
 *  level (how the fixtures and the machine's own chain are shaped) or under
 *  `promoted_by` (how the cloud records who pressed save). */
function actorDocumentOf(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  if (isRecord(value.acting)) return value;
  for (const nested of Object.values(value)) {
    if (isRecord(nested) && isRecord(nested.acting)) return nested;
  }
  return null;
}

/**
 * Who ran this, in the form a reader can act on: the person, and the agent
 * that acted inside their session when there was one — "ops@example.com via
 * sess-01J7Q3M8". A document this does not recognise is returned as-is by
 * {@link receiptValue} rather than reduced to a dash: an unreadable chain is
 * still evidence.
 */
export function principalSummary(value: unknown): string | null {
  const record = actorDocumentOf(value);
  if (record === null) return null;
  const acting = isRecord(record.acting) ? record.acting : {};
  const links = linksOf(record);
  const delegating = isRecord(record.delegating_user) ? record.delegating_user : null;
  const user =
    delegating ?? links.find((link) => link.kind === "user") ?? (acting.kind === "user" ? acting : null);
  const agent =
    (acting.kind === "agent" ? acting : null) ?? links.find((link) => link.kind === "agent") ?? null;
  const person = nameOf(user ?? acting);
  const via = agent ? nameOf(agent) : "";
  if (person && via && person !== via) return `${person} via ${via}`;
  return person || via || "";
}

/** The parameters this receipt's statement was bound with, or none.
 *
 *  Returned as pairs rather than a string: the panel renders them as a list,
 *  which is the difference between "customer: acme" and asking the reader to
 *  find the customer inside `{"customer":"acme"}` on the trust surface. */
export function receiptParams(receipt: Record<string, unknown>): FormattedParam[] {
  const value = receipt.params;
  return receiptUnknown(value) ? [] : formatParams(value);
}

/** One receipt field, rendered.
 *
 *  The three machine-shaped facts — when it ran, how many rows, how long it
 *  took — read through the SAME formatters the transcript's tool card uses
 *  (`@alkera/ui`), because a receipt exists to be compared with the card that
 *  produced it. An ISO string beside a locale timestamp is two clocks. */
export function receiptValue(receipt: Record<string, unknown>, key: string): string {
  const value = receipt[key];
  if (receiptUnknown(value)) return "—";
  if (key === "principal_chain") return principalSummary(value) || displayValue(value);
  if (key === "executed_at") return formatTimestamp(String(value));
  if (key === "row_count") return typeof value === "number" ? formatCount(value) : displayValue(value);
  if (key === "duration_ms") return typeof value === "number" ? formatDuration(value) : displayValue(value);
  // Parameters as one line, for a caller with no room for a list; the panel
  // itself renders `receiptParams` instead.
  if (key === "params") {
    const params = formatParams(value);
    return params.length === 0
      ? displayValue(value)
      : params.map((param) => `${param.name}: ${param.value}`).join(", ");
  }
  return displayValue(value);
}
