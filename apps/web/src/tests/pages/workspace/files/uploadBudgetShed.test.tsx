// What the tray says while the server has no room for a part.
//
// The backend caps the bytes it will hold in flight and sheds the rest with a
// `503` + `Retry-After` (`apps/backend/backend/api/body_limit.py`). The budget
// admits only a couple of max-size parts per process, so a third person
// uploading while two others are mid-part meets it routinely. The row must say
// it is waiting and then finish — a failure there is a file the person is told
// they lost, for a second's queueing.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { uploadConcurrency } from "@/api/filesUpload";
import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { lanesAllowed } from "@/pages/workspace/files/useUploads";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

const WRITABLE: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

const DRIVE = "drive-1";

/** The shed the body budget answers with, verbatim from `_shed_busy`. */
const BUSY = {
  error: {
    code: "unavailable",
    message: "The server is busy handling uploads; retry shortly",
    trace_id: "",
  },
};

function fileEntry(name: string): DropEntry {
  const file = new File(["id,name\n1,ok\n"], name, { lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

function transfer(entries: DropEntry[]): DropTransfer {
  return {
    items: entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
    getData: () => "",
  };
}

interface Wire {
  touched: { method: string; path: string }[];
}

/** The session API, with the first `sheds` PUTs of the part refused for room.
 *  `share` is what the server names as the caller's slice of the upload budget
 *  on every part answer it gives, shed or admitted. */
function stubFetch(sheds: number, share?: string): Wire {
  const wire: Wire = { touched: [] };
  let opened = 0;
  let queued = 0;
  let refused = 0;
  const accepted: number[] = [];

  const json = (payload: unknown, status = 200, headers: Record<string, string> = {}) =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json", ...headers },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const built = input instanceof Request ? input : null;
      const method = (init?.method ?? built?.method ?? "GET").toUpperCase();
      wire.touched.push({ method, path });

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        opened += 1;
        return json({ uploadId: `sess-${opened}`, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }

      const operation = /\/operations\/(op-\d+)$/.exec(path)?.[1];
      if (operation && method === "GET") {
        return json({
          id: operation,
          driveId: DRIVE,
          kind: "upload",
          state: "done",
          done: 1,
          total: 1,
          bytes: 0,
          skipped: 0,
          conflicts: [],
          errors: [],
          resultNodeId: null,
          resultVersionId: null,
          resultUnchanged: false,
        });
      }

      const sessionId = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      if (sessionId && path.endsWith("/complete")) {
        queued += 1;
        return json({ id: `op-${queued}`, kind: "upload", state: "queued", done: 0, total: 1 }, 202);
      }
      if (sessionId && method === "GET") {
        return json({
          uploadId: sessionId,
          state: "open",
          offset: accepted.length * 1024,
          length: 1024,
          complete: accepted.length > 0,
          partsDone: accepted.length,
          partsTotal: 1,
          acceptedParts: [...accepted],
        });
      }
      if (sessionId && method === "PUT") {
        const said: Record<string, string> = share === undefined ? {} : { "x-upload-concurrency": share };
        if (refused < sheds) {
          refused += 1;
          return json(BUSY, 503, { "retry-after": "1", ...said });
        }
        accepted.push(1);
        return json({ ok: true }, 200, said);
      }
      if (sessionId && method === "DELETE") return new Response(null, { status: 204 });
      if (method === "PATCH") return json({ id: "node", etag: "etag-2" });
      if (method === "GET" && /\/children$/.test(path)) {
        return json({ value: [], nextMarker: null });
      }
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
  return wire;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

function Harness({ entries }: { entries: DropEntry[] }) {
  const uploads = useUploads({
    driveId: DRIVE,
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(WRITABLE, transfer(entries))}>
        drop
      </button>
      <UploadTray
        rows={uploads.rows}
        skippedSidecars={uploads.skippedSidecars}
        identicalCopies={uploads.identicalCopies}
        alreadyInFiles={uploads.alreadyInFiles}
        finished={uploads.finished}
        refusal={uploads.refusal}
        batch={uploads.batch}
        conflicts={uploads.conflicts}
        resumable={uploads.resumable}
        onPause={uploads.pause}
        onResume={uploads.resume}
        onCancel={uploads.cancel}
        onAnswerConflict={uploads.answerConflict}
        onRetry={(id) => void uploads.retry(id)}
        onRetryFailed={() => void uploads.retryFailed()}
        onDismiss={uploads.dismiss}
      />
    </div>
  );
}

function renderTray(entries: DropEntry[]) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Harness entries={entries} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  uploadConcurrency.hint = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
  uploadConcurrency.hint = null;
});

describe("a part the server has no room for", () => {
  it("says the row is waiting and then uploads it, never that it failed", async () => {
    const wire = stubFetch(2);
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    await user.click(screen.getByRole("button", { name: "drop" }));

    // The wait is stated on the row rather than left as a stalled percentage.
    await screen.findByText("Waiting for the server…", undefined, { timeout: 5000 });
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("Uploaded")).toBeInTheDocument(), {
      timeout: 10_000,
    });
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    // One row, one session: the shed is waited out on the session already open,
    // not answered by starting the file over.
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(
      wire.touched.filter(
        (call) => call.method === "POST" && call.path.endsWith("/api/v1/files/uploads"),
      ),
    ).toHaveLength(1);
    expect(
      wire.touched.filter((call) => call.method === "PUT" && /\/parts\/1$/.test(call.path)),
    ).toHaveLength(3);
  }, 15_000);

  it("narrows the queue to the share the shed named, so the next drop is not shed too", async () => {
    stubFetch(1, "1");
    const user = userEvent.setup();
    renderTray([fileEntry("one.csv")]);
    expect(lanesAllowed()).toBe(3);

    await user.click(screen.getByRole("button", { name: "drop" }));
    await waitFor(() => expect(screen.getByText("Uploaded")).toBeInTheDocument(), {
      timeout: 10_000,
    });
    // The share came off the real part answer, through the upload client, and
    // is what the next drop opens lanes to.
    expect(lanesAllowed()).toBe(1);
  }, 15_000);
});
