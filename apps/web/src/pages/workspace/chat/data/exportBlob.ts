import type { BlobPage, BlobReference } from "@alkera/chat-model";
import { chatData, chatHost } from "./runtime";

// Page sizes when gathering a whole blob for export — the daemon's MAX per kind,
// so a large result downloads in as few round-trips as possible.
const ROW_FETCH = 1000;
const TEXT_FETCH = 50_000;
// Safety bound so a pathologically large blob can't OOM the webview.
const MAX_ROWS = 500_000;
const MAX_CHARS = 50_000_000;

/** Gather every page of a blob into a single result. Loops `fetchBlob` from the
 *  top until `hasMore` is false (or a safety bound is hit).
 *
 *  `truncated` says the bound was what stopped it — the file the reader is about
 *  to save is a PREFIX of the result. It was written silently before, so an
 *  analyst opened a half million rows believing they had the whole answer. */
async function gatherBlob(handle: string): Promise<{
  kind: "rows" | "text";
  columns: string[];
  rows: unknown[][];
  text: string;
  truncated: boolean;
}> {
  const first = await chatData().fetchBlob(handle, 0, kindFetchSize(undefined));
  const columns = first.columns;
  const rows: unknown[][] = [...first.rows];
  let text = first.text;
  let page: BlobPage = first;
  let truncated = false;
  while (page.hasMore && page.nextOffset !== null) {
    if (rows.length >= MAX_ROWS || text.length >= MAX_CHARS) {
      truncated = true;
      break;
    }
    page = await chatData().fetchBlob(handle, page.nextOffset, kindFetchSize(page.kind));
    if (page.kind === "rows") rows.push(...page.rows);
    else text += page.text;
  }
  return { kind: first.kind, columns, rows, text, truncated };
}

function kindFetchSize(kind: string | undefined): number {
  return kind === "text" ? TEXT_FETCH : ROW_FETCH;
}

// Cell prefixes a spreadsheet evaluates instead of displaying. Both the rows and
// the column names come from a warehouse query, i.e. third-party data, and this
// CSV is exactly the file an analyst opens in Excel.
const FORMULA_LEAD = /^[=+\-@\t\r]/;

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  // Numbers are values the query returned, never a formula — leave them numeric
  // so a negative figure doesn't become text in the spreadsheet.
  if (typeof value === "number" || typeof value === "bigint") return String(value);
  const raw = typeof value === "object" ? JSON.stringify(value) : String(value);
  // A formula-leading text cell gets an apostrophe, which spreadsheets strip on
  // display, so the reader still sees the original text.
  const text = FORMULA_LEAD.test(raw) ? `'${raw}` : raw;
  // Quote when the cell contains a delimiter, quote, or newline (RFC 4180).
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function toCsv(columns: string[], rows: unknown[][]): string {
  const head = columns.map(csvCell).join(",");
  const body = rows.map((row) => row.map(csvCell).join(",")).join("\n");
  return body ? `${head}\n${body}` : head;
}

function safeBaseName(reference: BlobReference): string {
  const name = reference.name.replace(/[^\w.-]+/gu, "-").replace(/^-+|-+$/gu, "").slice(0, 40);
  return `${name || "result"}-${reference.handle.slice(0, 8)}`;
}

/** What the reader is told when the saved file stopped short of the result. One
 *  sentence, because there is one thing to know and nothing to do about it here:
 *  the remaining rows are still in the chat, and a narrower query brings them
 *  within reach. */
export const EXPORT_TRUNCATED_NOTICE =
  "The result was too large to save in full, so this file holds the first part of it.";

/** Export a tool-result blob to a file: a `rows` result becomes CSV, a JSON blob
 *  stays JSON, any other text stays text. Routed through `chatHost().saveFile`, which
 *  opens a native Save dialog in VS Code (the user picks where it lands) and
 *  triggers a client-side download in the browser.
 *
 *  Answers whether the file it saved is the whole result, so the surface that
 *  offered the export can say when it is not. */
export async function exportBlob(reference: BlobReference): Promise<{ truncated: boolean }> {
  const all = await gatherBlob(reference.handle);
  let contents: string;
  let ext: string;
  let mimeType: string;
  if (all.kind === "rows") {
    contents = toCsv(all.columns, all.rows);
    ext = "csv";
    mimeType = "text/csv";
  } else if (reference.mime === "application/json") {
    contents = all.text;
    ext = "json";
    mimeType = "application/json";
  } else {
    contents = all.text;
    ext = "txt";
    mimeType = "text/plain";
  }
  await chatHost().saveFile(`${safeBaseName(reference)}.${ext}`, contents, mimeType);
  return { truncated: all.truncated };
}
