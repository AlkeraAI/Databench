import type { BlobPage } from "@alkera/chat-model";
import { afterEach, describe, expect, it, vi } from "vitest";
import { installChatRuntime } from "./runtime";
import type { ChatDataSource, ChatHost } from "./ChatDataSource";
import { EXPORT_TRUNCATED_NOTICE, exportBlob } from "./exportBlob";

// A host mock exposing `saveFile` (the unified export path — a Save dialog in VS
// Code, a client download in the browser) + `subscribe` (the DaemonDataSource
// constructor wires a host subscription when ./index loads). `vi.hoisted` so the
// mock exists before the hoisted `vi.mock` factory runs.
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

function rowsPage(rows: unknown[][], offset: number, total: number): BlobPage {
  const end = offset + rows.length;
  return {
    kind: "rows",
    columns: ["id", "label"],
    rows,
    text: "",
    offset,
    limit: 1000,
    total,
    returned: rows.length,
    hasMore: end < total,
    nextOffset: end < total ? end : null,
  };
}

function textPage(text: string): BlobPage {
  return { kind: "text", columns: [], rows: [], text, offset: 0, limit: 50000, total: text.length, returned: text.length, hasMore: false, nextOffset: null };
}

afterEach(() => {
  vi.restoreAllMocks();
  fetchBlob.mockReset();
  hostMock.saveFile.mockReset();
});

describe("exportBlob", () => {
  it("saves a rows blob as CSV named from the result + handle", async () => {
    fetchBlob.mockResolvedValue(
      rowsPage([[0, "row-0"], [1, "row-1"]], 0, 2),
    );
    await exportBlob({ handle: "abcdef0123456789", name: "Q3 revenue", refType: "rows" });
    expect(hostMock.saveFile).toHaveBeenCalledTimes(1);
    const [filename, contents, mimeType] = hostMock.saveFile.mock.calls[0];
    expect(filename).toBe("Q3-revenue-abcdef01.csv");
    expect(contents).toBe("id,label\n0,row-0\n1,row-1");
    expect(mimeType).toBe("text/csv");
  });

  it("gathers every page and CSV-quotes cells with delimiters", async () => {
    fetchBlob.mockImplementation(async (_h, offset) =>
      offset === 0
        ? rowsPage([[0, "a,b"]], 0, 2) // a comma → must be quoted
        : rowsPage([[1, 'has "quote"']], 1, 2),
    );
    await exportBlob({ handle: "h".repeat(16), name: "x", refType: "rows" });
    expect(hostMock.saveFile.mock.calls[0][1]).toBe('id,label\n0,"a,b"\n1,"has ""quote"""');
  });

  it("saves a JSON text blob as .json and other text as .txt", async () => {
    fetchBlob.mockResolvedValue(textPage('{"a":1}'));
    await exportBlob({ handle: "j".repeat(16), name: "doc", refType: "text", mime: "application/json" });
    expect(hostMock.saveFile.mock.calls[0][0]).toBe("doc-jjjjjjjj.json");
    expect(hostMock.saveFile.mock.calls[0][2]).toBe("application/json");

    hostMock.saveFile.mockReset();
    await exportBlob({ handle: "t".repeat(16), name: "log", refType: "text" });
    expect(hostMock.saveFile.mock.calls[0][0]).toBe("log-tttttttt.txt");
    expect(hostMock.saveFile.mock.calls[0][2]).toBe("text/plain");
  });
});

// A blob bigger than the browser will hold. The export stops at its safety
// bound, and the reader has to be told: a file that is silently a PREFIX of the
// answer is the one failure an analyst cannot see in the spreadsheet.
describe("an export that runs into the safety bound", () => {
  it("says the file holds only the first part of the result", async () => {
    // Every page claims more behind it, so only the bound can stop the loop.
    fetchBlob.mockImplementation(async (_handle, offset = 0) =>
      rowsPage(
        Array.from({ length: 1000 }, (_, i) => [offset + i, "row"]),
        offset,
        10_000_000,
      ),
    );

    const result = await exportBlob({ handle: "h1", name: "Orders" });

    expect(result.truncated).toBe(true);
    expect(hostMock.saveFile).toHaveBeenCalledTimes(1);
    expect(EXPORT_TRUNCATED_NOTICE).toMatch(/first part/i);
  });

  it("reports a whole result as whole", async () => {
    fetchBlob.mockResolvedValueOnce(rowsPage([[1, "a"]], 0, 1));

    const result = await exportBlob({ handle: "h1", name: "Orders" });

    expect(result.truncated).toBe(false);
  });
});
