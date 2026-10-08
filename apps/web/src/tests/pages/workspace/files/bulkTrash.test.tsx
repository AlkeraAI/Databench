// A long selection is trashed as batches, not as one request per row.
//
// `Select all N` can now arm every row of a folder, and Move to trash fired one
// `DELETE …/items/{id}` per row in a single pass — 3,200 rows, 3,200 requests,
// each taking a rate-limit slot. The server already offers the batch: one
// `POST …/bulk` carries up to a thousand `trash` items, and a batch over the
// inline ceiling comes back as a queued operation with progress and a cancel.
//
// A short selection keeps the per-row path on purpose: each row is then its own
// operation, so undoing one does not drag the rest back.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { etagVersion, type BulkTrashResult, type Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import {
  FilesActions,
  batchOutcome,
  batchSizes,
  type FilesOperationReport,
} from "@/pages/workspace/files/FilesActions";
import { undoStep } from "@/pages/workspace/files/undo";
import { FILES_BULK_BATCH_ITEMS, FILES_BULK_INLINE_ITEMS } from "@/lib/limits";

const DRIVE = "drv_1";
const FOLDER = "nd_parent";

function item(id: string): Item {
  return {
    id,
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    name: `${id}.txt`,
    nameDisplay: `${id}.txt`,
    nameEncoding: "utf-8",
    pathBytes: "",
    parentId: FOLDER,
    etag: String(id.replace("nd_", "") || 0),
    ctag: `ct-${id}`,
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: {
      can_read: true,
      can_write: true,
      can_share: true,
      can_delete: true,
      can_rename: true,
      can_download: true,
      can_lease: false,
      can_lease_request: false,
      can_lease_force: false,
    },
  } as Item;
}

const rows = (count: number): Item[] => Array.from({ length: count }, (_, at) => item(`nd_${at}`));

let wire: Request[] = [];

/** Per-item statuses to answer a batch with, keyed by the item's position in it.
 *  Absent means every row applied. */
let refusals: Record<number, { status: number; code: string }> = {};

/**
 * The stub answers a batch the way the route does, which the first version of
 * this file did not: a body at or under the server's inline ceiling is applied
 * in the request and comes back as per-item rows with NO operation, and only a
 * longer one is queued as a 202. A stub that always answered 202 could not see
 * the difference, which is how a trailing chunk under the ceiling went unnoticed.
 */
function stubFetch(): void {
  wire = [];
  refusals = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request = input as Request;
      wire.push(request);
      if (request.url.includes("/bulk")) {
        const body = (await request.clone().json()) as { items: { id: string }[] };
        if (body.items.length > FILES_BULK_INLINE_ITEMS) {
          // A batch is filed under its own kind and records no inverse, so
          // the server publishes it as one it will not undo.
          return new Response(
            JSON.stringify({ id: "op_1", kind: "bulk", state: "running", undoable: false }),
            { status: 202, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            responses: body.items.map((row, at) =>
              refusals[at]
                ? { id: row.id, status: refusals[at]!.status, body: { code: refusals[at]!.code } }
                : { id: row.id, status: 204, body: null },
            ),
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (request.method === "DELETE") {
        // The single-row route runs the trash AS the operation its undo
        // inverts, and answers 200 with that operation.
        return new Response(
          JSON.stringify({ id: "op_row", kind: "trash", state: "done", undoable: true }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function Page({
  selection,
  reports,
}: {
  selection: readonly Item[];
  reports?: FilesOperationReport[];
}) {
  return (
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <FilesActions canWriteHere
          driveId={DRIVE}
          currentFolderId={FOLDER}
          selection={selection}
          platform="other"
          onOperation={(report) => reports?.push(report)}
        >
          {({ triggerProps }) => (
            <div data-testid="grid" {...triggerProps}>
              <div tabIndex={0} data-testid="row">
                rows
              </div>
            </div>
          )}
        </FilesActions>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/** Move to trash, through the keyboard the selection bar and the menu share. */
function trashTheSelection(): void {
  fireEvent.keyDown(screen.getByTestId("grid"), { key: "Delete" });
}

const bulkCalls = () => wire.filter((r) => r.url.includes("/bulk"));
const deleteCalls = () => wire.filter((r) => r.method === "DELETE");

beforeEach(() => stubFetch());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("trashing a long selection", () => {
  it("sends batches, not one request per row", async () => {
    const selection = rows(FILES_BULK_BATCH_ITEMS * 2 + 5);
    render(<Page selection={selection} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(3));
    expect(deleteCalls()).toEqual([]);
  });

  it("carries every row exactly once, with the etag it was read at", async () => {
    const selection = rows(FILES_BULK_BATCH_ITEMS + 3);
    render(<Page selection={selection} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(2));
    const sent: { itemId: string; ifMatch: string; op: string }[] = [];
    for (const call of bulkCalls()) {
      const body = (await call.clone().json()) as {
        items: { op: string; itemId: string; ifMatch: string }[];
      };
      expect(body.items.length).toBeLessThanOrEqual(FILES_BULK_BATCH_ITEMS);
      sent.push(...body.items);
    }
    expect(sent.map((row) => row.itemId)).toEqual(selection.map((row) => row.id));
    expect(new Set(sent.map((row) => row.op))).toEqual(new Set(["trash"]));
    // The batch fences per item on the etag as the counter it is, which is the
    // same value the single-row route sends as its `If-Match` header.
    expect(sent.map((row) => row.ifMatch)).toEqual(
      selection.map((row) => etagVersion(row.etag)),
    );
  });

  it("gives each batch its own idempotency key", async () => {
    render(<Page selection={rows(FILES_BULK_BATCH_ITEMS + 1)} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(2));
    const keys = bulkCalls().map((call) => call.headers.get("Idempotency-Key"));
    expect(keys.every((key) => typeof key === "string" && key !== "")).toBe(true);
    expect(new Set(keys).size).toBe(2);
  });

  it("leaves a short selection on the per-row path, one DELETE each", async () => {
    // The asymmetric case: batching everything would make a two-row trash one
    // operation, and undoing it would drag back a row the reader did not ask for.
    const selection = rows(FILES_BULK_INLINE_ITEMS);
    render(<Page selection={selection} />);
    trashTheSelection();

    await waitFor(() => expect(deleteCalls().length).toBe(FILES_BULK_INLINE_ITEMS));
    expect(bulkCalls()).toEqual([]);
    expect(deleteCalls()[0]?.headers.get("If-Match")).toBe("0");
  });

  it("switches to batches at the first row past the inline ceiling", async () => {
    render(<Page selection={rows(FILES_BULK_INLINE_ITEMS + 1)} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(1));
    expect(deleteCalls()).toEqual([]);
  });

  it("stays on the per-row path when a row's etag is not a counter it can fence on", async () => {
    // Fail closed: the batch item carries the counter, not the header, so a row
    // whose etag is some other shape cannot ride it. Going out unfenced would be
    // a blind write over every row that moved underneath the reader.
    const selection = rows(FILES_BULK_INLINE_ITEMS + 5).map((row, at) =>
      at === 3 ? ({ ...row, etag: 'W/"opaque"' } as Item) : row,
    );
    render(<Page selection={selection} />);
    trashTheSelection();

    await waitFor(() => expect(deleteCalls().length).toBe(selection.length));
    expect(bulkCalls()).toEqual([]);
  });

  it("still batches when every etag is a weak validator", async () => {
    // The positive twin: `W/"7"` is the same counter, so it rides the batch.
    const selection = rows(FILES_BULK_INLINE_ITEMS + 2).map(
      (row) => ({ ...row, etag: `W/"${row.etag}"` }) as Item,
    );
    render(<Page selection={selection} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(1));
    const body = (await bulkCalls()[0]!.clone().json()) as { items: { ifMatch: number }[] };
    expect(body.items[0]?.ifMatch).toBe(0);
    expect(deleteCalls()).toEqual([]);
  });

  it("trashes one row with one request", async () => {
    render(<Page selection={rows(1)} />);
    trashTheSelection();

    await waitFor(() => expect(deleteCalls().length).toBe(1));
    expect(bulkCalls()).toEqual([]);
  });
});

describe("what a batch tells the reader afterwards", () => {
  it("does not put a queued batch on the undo stack, because the server will not undo it", async () => {
    // The batch route starts its operation with no inverse on purpose, so
    // `POST \u2026/operations/{id}/undo` answers 409 files.not_undoable -- and the
    // operation says so on the wire. The report carries the server's own answer
    // and `undoStep` declines it; nothing here decides that a batch is special.
    const reports: FilesOperationReport[] = [];
    render(<Page selection={rows(FILES_BULK_INLINE_ITEMS + 5)} reports={reports} />);
    trashTheSelection();

    await waitFor(() => expect(reports.length).toBe(1));
    const report = reports[0]!;
    expect(report.operationId).toBe("op_1");
    expect(report.undoable).toBe(false);
    expect(undoStep({ ...report, driveId: DRIVE })).toBeNull();
    // And it does not claim the rows have moved: a queued batch has been
    // accepted, and the runner walks it afterwards.
    expect(report.label).toBe("Moving 105 items to trash");
  });

  it("still offers an undo on the per-row path, which has a real inverse", async () => {
    // The positive twin, and the asymmetry that matters: one row answers with
    // the operation its undo inverts, the server marks it undoable, and that
    // one must still become a step.
    const reports: FilesOperationReport[] = [];
    render(<Page selection={rows(1)} reports={reports} />);
    trashTheSelection();

    await waitFor(() => expect(reports.length).toBe(1));
    const report = reports[0]!;
    expect(report.undoable).toBe(true);
    expect(undoStep({ ...report, driveId: DRIVE })?.operationId).toBe("op_row");
  });

  it("splits a selection evenly, so no chunk falls under the inline ceiling", async () => {
    // 1,050 rows chunked at the maximum is [1000, 50], and the 50 comes back
    // inline with no operation behind it. Even chunks keep every batch above
    // the ceiling and below the cap.
    render(<Page selection={rows(FILES_BULK_BATCH_ITEMS + 50)} />);
    trashTheSelection();

    await waitFor(() => expect(bulkCalls().length).toBe(2));
    const sizes: number[] = [];
    for (const call of bulkCalls()) {
      const body = (await call.clone().json()) as { items: unknown[] };
      sizes.push(body.items.length);
    }
    expect(sizes).toEqual([525, 525]);
    for (const size of sizes) {
      expect(size).toBeGreaterThan(FILES_BULK_INLINE_ITEMS);
      expect(size).toBeLessThanOrEqual(FILES_BULK_BATCH_ITEMS);
    }
  });
});

describe("how a selection is split", () => {
  it.each([
    [1, [1]],
    [101, [101]],
    [FILES_BULK_BATCH_ITEMS, [FILES_BULK_BATCH_ITEMS]],
    [FILES_BULK_BATCH_ITEMS + 1, [501, 500]],
    [FILES_BULK_BATCH_ITEMS + 50, [525, 525]],
    [3200, [800, 800, 800, 800]],
  ])("splits %i rows into %j", (count, expected) => {
    expect(batchSizes(count)).toEqual(expected);
  });

  it("never emits a chunk the server would answer inline", () => {
    // The property the even split exists for. Filling from the front produces a
    // tail of `count % 1000`, which is under the ceiling for most counts.
    for (let count = FILES_BULK_INLINE_ITEMS + 1; count < 6000; count += 1) {
      const sizes = batchSizes(count);
      expect(sizes.reduce((a, b) => a + b, 0)).toBe(count);
      for (const size of sizes) {
        expect(size).toBeGreaterThan(FILES_BULK_INLINE_ITEMS);
        expect(size).toBeLessThanOrEqual(FILES_BULK_BATCH_ITEMS);
      }
    }
  });
});

describe("reading what a batch answered", () => {
  const batch = rows(4);

  it("counts a queued batch as asked, because it has not run yet", () => {
    const queued = { id: "op_1", state: "running" } as unknown as BulkTrashResult;
    expect(batchOutcome(queued, batch)).toEqual({ moved: batch, refused: [] });
  });

  it("separates the rows that applied from the rows that were refused", () => {
    // One row's stale etag is that row's 412 while its neighbours commit, so an
    // answered batch is a partial success and the rows are the only record.
    const answered: BulkTrashResult = {
      responses: [
        { id: "t0", status: 204, body: null },
        { id: "t1", status: 412, body: { code: "files.precondition_failed" } },
        { id: "t2", status: 204, body: null },
        { id: "t3", status: 403, body: { code: "files.forbidden" } },
      ],
    };
    const { moved, refused } = batchOutcome(answered, batch);
    expect(moved.map((row) => row.id)).toEqual(["nd_0", "nd_2"]);
    expect(refused.map((row) => row.status)).toEqual([412, 403]);
  });

  it("reports nothing moved when every row was refused", () => {
    const answered: BulkTrashResult = {
      responses: batch.map((_, at) => ({
        id: `t${at}`,
        status: 412,
        body: { code: "files.precondition_failed" },
      })),
    };
    expect(batchOutcome(answered, batch).moved).toEqual([]);
  });
});
